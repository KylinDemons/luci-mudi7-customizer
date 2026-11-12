#!/usr/bin/env python3

import fcntl
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import time

ROOT = Path('/etc/mudi7-customizer')
CONFIG = ROOT / 'battery_config.json'
STATE = Path('/var/run/mudi7-customizer/battery.json')
LOCK = Path('/var/run/mudi7-customizer/battery.lock')
DAEMON_LOCK = LOCK.with_name('battery-daemon.lock')
RESTORE = LOCK.with_name('battery-vreg.json')
STAMP = LOCK.with_name('battery-poll.log')
SERVICE = '/etc/init.d/batterycontrol'
BASE = Path('/sys/class/power_supply')
BAT = BASE / 'cw221X-bat'
PUMP = BASE / 'sgm41600-standalone/device/sgm41600/charge_en'
VREG = BASE / 'charger/device/sgm41542s/vreg'
CHGEN = BASE / 'charger/device/sgm41542s/charge_en'
ONLINE = BASE / 'charger/online'
CEILING_HEADROOM_UV = 0     # vreg sits AT the cell voltage; a CV below it makes
                            # the buck terminate and cut its input (measured)
CEILING_FLOOR_UV = 3900000  # never gate below the reference project's floor
QUIET_MA = 50               # |cell current| that still counts as "idle", i.e.
                            # a voltage reading without I*R offset
TRACK_DOWN_DEADBAND_UV = 10000  # ignore sub-10 mV downward drift when tracking
FAST_POLL = 2               # s, guard window after the gate is (re)applied
SLOW_POLL = 10              # s, steady state while the gate is holding
ARM_POLL = 5                # s, adapter present, still below the threshold
IDLE_POLL = 30              # s, no adapter (or limit off): heartbeat only
GUARD_WINDOW = 30           # s of calm before backing off to SLOW_POLL
MIN_TICK_GAP = 1.0          # s, floor between ticks so a uevent burst cannot spin
UEVENT_NETLINK = 15         # NETLINK_KOBJECT_UEVENT (not exported by socket)
UEVENT_GROUP = 1            # kernel multicast group for udev-style events
# Which uevents are worth a full tick. The CW221X fuel gauge re-broadcasts a
# 'change' event roughly every 10 s whether or not anything happened, so a
# blanket power_supply filter would wake us at poll rate and defeat the whole
# point. Only adapter-side changes (and device add/remove) can alter what the
# controller has to do; gauge events are drained and ignored, and capacity is
# still re-read on the timer.
WAKE_MARKERS = (b'power_supply/charger', b'sgm41600', b'/typec/')
DEFAULT = dict(enabled=False, threshold=80)


def read(path):
    return path.read_text().strip()


def load(path, default):
    try:
        return dict(default, **json.loads(path.read_text()))
    except (OSError, ValueError, TypeError):
        return dict(default)


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.new')
    with tmp.open('w') as f:
        json.dump(value, f)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def supported():
    return all(p.exists() for p in (BAT / 'capacity', BAT / 'voltage_now', ONLINE, PUMP, VREG, CHGEN))


def gauge_to_display(capacity):
    # GL's display uses a different scale from the CW221X gauge.
    return max(0, min(100, (capacity * 13867 - 189300 + 5000) // 10000))


def release():
    if not RESTORE.exists():
        return
    saved = load(RESTORE, {})
    original = saved.get('voltage')
    if not isinstance(original, int) or not 3000000 <= original <= 4400000:
        raise RuntimeError('Invalid saved charger voltage')
    # Restore the CV first so the moment charging resumes it targets the
    # factory voltage, then re-enable the buck. The pump is left alone:
    # switching it back is the vendor daemon's job on the next plug event.
    if int(read(VREG)) != original:
        VREG.write_text(f'{original}\n')
        if int(read(VREG)) != original:
            raise RuntimeError('Charger did not restore voltage')
    if read(CHGEN) != '1':
        CHGEN.write_text('1\n')
    RESTORE.unlink()


def apply_gate(voltage, current):
    """Force the charge gate.

    Returns True only when the gate had to be *(re)established* — the first
    application, or the CV moved while our own target stayed put. Two things
    are deliberately excluded, because counting either one keeps the guard
    window alive forever and 2 s polling never backs off to 10 s:

      * a CV step we made ourselves, tracking the cell's falling OCV;
      * the vendor daemon flipping charge_en back to 1. Measured while the
        pack was held: 9 such rewrites in 20 s, yet the cell still drew 0 mA
        because the CV was sitting on its open-circuit voltage.
    """
    saved = load(RESTORE, {})
    first = 'target' not in saved
    if first:
        factory = int(read(VREG))
        if not 3000000 <= factory <= 4400000:
            raise ValueError('Invalid charger voltage')
        # Seed from the factory CV and the instantaneous cell voltage. This
        # reading can be inflated by I*R if the cell was still charging when
        # the gate closed, which is the safe direction (a CV above the cell
        # never cuts the buck input); the resting-voltage tracker below pulls
        # it onto the real open-circuit voltage within one tick.
        target = max(CEILING_FLOOR_UV, min(factory, (voltage + CEILING_HEADROOM_UV) // 1000 * 1000))
        atomic(RESTORE, {'voltage': factory, 'target': target})
        saved = load(RESTORE, {})
    target = saved['target']
    # Keep the CV locked on the cell's OPEN-CIRCUIT voltage:
    #  - below it, the buck terminates and cuts its input on every
    #    quec_battery pulse (measured: ibus=0, -200..-250 mA on the cell);
    #  - above it, those same pulses become real charging.
    # Open-circuit voltage can only be read while the cell is idle, so samples
    # taken during a pulse (I*R offset, either polarity) are ignored. Tracking
    # is then two-way, which is safe precisely because idle samples carry no
    # load sag -- this is not the old ratchet, which re-derived the target from
    # the cell voltage while it was being dragged down by the load.
    cap = saved.get('voltage', 4400000)
    if not isinstance(cap, int) or not 3000000 <= cap <= 4400000:
        cap = 4400000
    touched = first
    tracked = False
    if abs(current) <= QUIET_MA:
        resting = max(CEILING_FLOOR_UV, min(cap, voltage // 1000 * 1000))
        # Follow upward immediately (a CV under the cell is what cuts the buck
        # input) but only step down on a real move, so 1 mV gauge jitter cannot
        # walk the target down tick after tick.
        if resting > target or resting < target - TRACK_DOWN_DEADBAND_UV:
            target = resting
            saved['target'] = target
            atomic(RESTORE, saved)
            tracked = True
    if read(PUMP) != '0':
        PUMP.write_text('0\n')
    if read(CHGEN) != '0':
        CHGEN.write_text('0\n')
        # Deliberately NOT counted as interference. quec_battery rewrites this
        # bit several times a minute while the pack sits at its threshold; if
        # each rewrite restarted the guard window, 2 s polling would never back
        # off (measured: 9 re-asserts in 20 s, guard held 126 s through a 30 s
        # window). It costs nothing to lose this race: over 135 samples taken
        # while the vendor daemon had forced charge_en back to 1, the cell drew
        # 0 mA in 123 of them and 30-312 mA transients in the rest, with the
        # controller reporting term-charge. What actually holds the current at
        # zero is the CV sitting on the cell's open-circuit voltage.
    if int(read(VREG)) != target:
        VREG.write_text(f'{target}\n')
        # A CV that disagrees with an unchanged target means somebody else
        # moved it; a CV we are stepping ourselves is not interference. The
        # very first application also lands here (tracked is False then), which
        # is correct — that one does deserve a fresh guard window.
        if not tracked:
            touched = True
    # Only the CV is worth verifying. charge_en being toggled back by the
    # vendor daemon must not raise: an exception sets failed=True, which
    # restarts the guard window exactly like a re-assert would, and this race
    # is unwinnable by design (see above).
    if int(read(VREG)) != target:
        raise RuntimeError('Charger did not hold the charge limit')
    return touched


def tick(cfg):
    if not supported():
        raise RuntimeError('Unsupported charger interfaces')
    gauge = int(read(BAT / 'capacity'))
    if not 0 <= gauge <= 100:
        raise ValueError('Invalid battery capacity')
    cap = gauge_to_display(gauge)
    online = read(ONLINE) == '1'
    enabled = cfg['enabled']
    block = enabled and online and cap >= cfg['threshold']
    if block:
        voltage = int(read(BAT / 'voltage_now'))
        if not 3000000 <= voltage <= 4500000:
            raise ValueError('Invalid battery voltage')
        current = int(read(BAT / 'current_now'))
        reasserted = apply_gate(voltage, current)
    else:
        release()
        reasserted = False
    return dict(capacity=cap, gauge_capacity=gauge,
                online=online, enabled=enabled, blocked=block,
                reasserted=reasserted,
                message='Charge held' if block else 'Armed' if enabled else 'Limit off',
                updated=time.time(), error='')


def operate(action, value=None):
    with LOCK.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        stored = load(CONFIG, DEFAULT)
        cfg = {key: stored[key] for key in DEFAULT}
        if action != 'tick':
            if not supported():
                raise RuntimeError('Unsupported charger interfaces')
            if action == 'enable':
                if value not in ('0', '1'):
                    raise ValueError('Enable must be 0 or 1')
                cfg['enabled'] = value == '1'
            elif action in ('threshold', 'configure'):
                if action == 'configure':
                    enabled, value = value
                    if type(enabled) is not bool:
                        raise ValueError('Enable must be a boolean')
                    cfg['enabled'] = enabled
                value = int(value)
                if not 50 <= value <= 100:
                    raise ValueError('Threshold must be 50–100')
                cfg['threshold'] = value
            else:
                raise ValueError('Invalid action')
            atomic(CONFIG, cfg)
        try:
            state = tick(cfg)
        except Exception as exc:
            try:
                release()
            except (OSError, ValueError, RuntimeError) as restore_error:
                exc = RuntimeError(f'{exc}; restore failed: {restore_error}')
            state = dict(error=str(exc), updated=time.time(), blocked=False)
        atomic(STATE, state)
        if state.get('error'):
            raise RuntimeError(state['error'])
        return state


def uevent_open():
    """Listen for kernel power-supply events; None if unavailable.

    The kernel broadcasts a uevent whenever a power_supply device changes, which
    covers plug/unplug and gauge updates. Waiting on it lets the daemon react to
    a plug-in immediately while doing *nothing* in between, instead of polling.
    """
    try:
        sock = socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM, UEVENT_NETLINK)
        sock.bind((os.getpid(), UEVENT_GROUP))
        sock.setblocking(False)
        return sock
    except OSError:
        return None


def uevent_drain(sock, markers=WAKE_MARKERS):
    """Consume queued uevents; return the first one worth acting on, if any."""
    hit = None
    while True:
        try:
            data = sock.recv(8192)
        except (BlockingIOError, InterruptedError):
            return hit
        except OSError:
            return hit
        if hit is None and any(m in data for m in markers):
            hit = data.split(b'\x00', 1)[0].decode('utf-8', 'replace')[:100]
    return hit


def wait_for_change(sock, timeout):
    """Sleep up to `timeout` seconds; return the uevent that woke us, if any."""
    if sock is None:
        time.sleep(timeout)
        return None
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        try:
            ready, _, _ = select.select([sock], [], [], left)
        except OSError:
            time.sleep(left)
            return None
        if not ready:
            return None
        woke = uevent_drain(sock)
        if woke:
            return woke


def trace(mode, interval, woke):
    """Append one poll decision to a small tmpfs log (diagnostics only)."""
    try:
        STAMP.parent.mkdir(parents=True, exist_ok=True)
        if STAMP.exists() and STAMP.stat().st_size > 64 * 1024:
            STAMP.write_text('')
        with STAMP.open('a') as f:
            f.write(f'{time.strftime("%H:%M:%S")} mode={mode} next={interval}s wake={woke or "-"}\n')
    except OSError:
        pass


def daemon_running():
    with DAEMON_LOCK.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False


def ensure_daemon():
    if daemon_running():
        return
    subprocess.run([SERVICE, 'enable'], check=True, stdout=subprocess.DEVNULL)
    subprocess.run([SERVICE, 'start'], check=True, stdout=subprocess.DEVNULL)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if daemon_running():
            return
        time.sleep(0.05)
    raise RuntimeError('Battery control service did not start')


def main():
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    if sys.argv[1:] == ['daemon']:
        # procd owns this service; an additional process lock prevents duplicates.
        with DAEMON_LOCK.open('a') as daemon_lock:
            fcntl.flock(daemon_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            def stop(*_):
                raise SystemExit(0)
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            events = uevent_open()
            try:
                gate_at = time.monotonic()
                last = 0.0
                while True:
                    # Floor the tick rate: a burst of kernel events must never
                    # turn into a spin loop.
                    gap = time.monotonic() - last
                    if gap < MIN_TICK_GAP:
                        time.sleep(MIN_TICK_GAP - gap)
                    last = time.monotonic()
                    failed = False
                    try:
                        state = operate('tick')
                    except Exception:
                        failed = True  # unsure - stay alert
                        state = load(STATE, {})
                    now = time.monotonic()
                    if failed or state.get('reasserted'):
                        # The gate was (re)applied or something went wrong:
                        # poll fast for a while in case the vendor daemon
                        # quec_battery tries to re-enable charging.
                        gate_at = now
                    if state.get('blocked') or failed:
                        calm = now - gate_at < GUARD_WINDOW
                        delay = FAST_POLL if calm else SLOW_POLL
                        mode = 'guard' if calm else 'hold'
                        time.sleep(delay)
                        woke = None
                    elif state.get('online'):
                        # Charging normally towards the threshold: nothing to
                        # defend yet, but the crossing must be caught promptly.
                        delay, mode = ARM_POLL, 'arming'
                        woke = wait_for_change(events, delay)
                    else:
                        # No adapter: gating is physically impossible, so there
                        # is nothing to poll for. Sleep until the kernel says
                        # something changed (plug-in, gauge update) and keep
                        # only a slow heartbeat in case that socket goes quiet.
                        delay, mode = IDLE_POLL, 'idle'
                        woke = wait_for_change(events, delay)
                    trace(mode, delay, woke)
            finally:
                with LOCK.open('a') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    try:
                        release()
                    finally:
                        state = load(STATE, {})
                        state.update(updated=0, blocked=False)
                        atomic(STATE, state)
    else:
        ensure_daemon()
        operate(*sys.argv[1:])


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
