#!/usr/bin/env python3
"""Configure and manage the quec-battery charging provider."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path('/etc/mudi7-customizer')
CONFIG = ROOT / 'battery-limit'
LEGACY_CONFIG = ROOT / 'battery_config.json'
ENABLED = ROOT / 'battery-provider.enabled'
RUNTIME = Path('/var/run/mudi7-customizer')
STATE = RUNTIME / 'battery-provider.json'
TRIAL = RUNTIME / 'battery-provider.trial'
LOCK = RUNTIME / 'battery.lock'
DAEMON_LOCK = RUNTIME / 'battery-daemon.lock'
LEGACY_RESTORE = RUNTIME / 'battery-vreg.json'

DASH = Path('/usr/libexec/mudi7-customizer')
PROVIDER = DASH / 'quec_battery'
LEGACY_PROVIDER = DASH / 'quec_battery2'
LAUNCHER = DASH / 'charge-launch.sh'
LIVE = Path('/usr/bin/quec_battery')
STOCK = LIVE.with_name('quec_battery.mudi7-stock')
SERVICE = '/etc/init.d/charge.init'
BASE = Path('/sys/class/power_supply')
BAT = BASE / 'cw221X-bat'
ONLINE = BASE / 'charger/online'
MODEL = Path('/tmp/sysinfo/model')
PUMP = BASE / 'sgm41600-standalone/device/sgm41600/charge_en'
VREG = BASE / 'charger/device/sgm41542s/vreg'
CHGEN = BASE / 'charger/device/sgm41542s/charge_en'
DEFAULT = {'enabled': False, 'threshold': 80}


def read(path):
    return path.read_text().strip()


def load(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def gauge_to_display(capacity):
    return max(0, min(100, (capacity * 13867 - 189300 + 5000) // 10000))


def gauge_threshold(display):
    return next(value for value in range(101) if gauge_to_display(value) >= display)


def configuration():
    try:
        display, gauge = map(int, read(CONFIG).split())
        if 50 <= display <= 100 and gauge in (0, gauge_threshold(display)):
            return {'enabled': gauge > 0, 'threshold': display}
    except (OSError, ValueError):
        pass
    legacy = load(LEGACY_CONFIG)
    display = legacy.get('threshold')
    if type(display) is int and 50 <= display <= 100:
        return {'enabled': legacy.get('enabled') is True, 'threshold': display}
    return DEFAULT.copy()


def save(enabled, threshold):
    ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=ROOT, delete=False) as output:
        temp = Path(output.name)
        try:
            output.write(f'{threshold} {gauge_threshold(threshold) if enabled else 0}\n')
            output.flush()
            os.fsync(output.fileno())
            temp.replace(CONFIG)
        finally:
            temp.unlink(missing_ok=True)


@contextmanager
def locked(path):
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with path.open('a') as lock:
        deadline = time.monotonic() + 8
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('Another charge limit operation is still running.')
                time.sleep(0.1)
        yield


def process_binary(pid):
    try:
        return os.readlink(f'/proc/{pid}/exe').removesuffix(' (deleted)')
    except OSError:
        return ''


def provider_selected():
    try:
        return LIVE.readlink() == LAUNCHER
    except OSError:
        return False


def supported():
    return (provider_selected() and ENABLED.exists() and PROVIDER.is_file() and
            all(path.exists() for path in (BAT / 'capacity', ONLINE)))


def provider_state():
    state = load(STATE)
    pid = state.get('pid')
    updated = state.get('updated')
    if (type(pid) is int and pid > 1 and type(updated) is int and
            -1 <= time.time() - updated <= 12 and process_binary(pid) == str(PROVIDER)):
        return state
    return {}


def snapshot():
    cfg = configuration()
    state = provider_state() if supported() else {}
    requested = gauge_threshold(cfg['threshold']) if cfg['enabled'] else 0
    if not state or state.get('gauge_stop') != requested:
        return cfg, {}
    return cfg, {
        'blocked': bool(state.get('blocked')),
        'online': bool(state.get('online')),
        'message': ('Limit off' if not cfg['enabled'] else
                    'Charge held' if state['blocked'] else 'Armed'),
        'error': '' if state.get('valid') else 'Battery telemetry unavailable',
    }


def wait_for_state(gauge, timeout, require_valid=False):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = provider_state()
        if (state.get('gauge_stop') == gauge and state.get('watchdog') in (1, 2)
                and (not require_valid or state.get('valid'))):
            return state
        time.sleep(0.1)
    raise RuntimeError('The charging daemon did not confirm the settings.')


def operate(action, value):
    with locked(LOCK):
        if not supported():
            raise RuntimeError('The quec-battery charging provider is unavailable.')
        cfg = configuration()
        enabled, threshold = cfg['enabled'], cfg['threshold']
        if action == 'configure':
            enabled, threshold = value
        elif action == 'enable' and value in ('0', '1'):
            enabled = value == '1'
        elif action == 'threshold':
            threshold = int(value)
        else:
            raise ValueError('Invalid charge limit action.')
        if type(enabled) is not bool or type(threshold) is not int or not 50 <= threshold <= 100:
            raise ValueError('The charge target must be an integer from 50 to 100.')
        save(enabled, threshold)
        state = wait_for_state(gauge_threshold(threshold) if enabled else 0, 8)
        if enabled and not state.get('valid'):
            raise RuntimeError('The setting was saved, but battery telemetry is unavailable.')


def service(action):
    return subprocess.run([SERVICE, action], timeout=8).returncode


def charging_daemon_runs(*binaries):
    paths = {str(binary) for binary in binaries}
    return any(process_binary(entry.name) in paths
               for entry in Path('/proc').iterdir() if entry.name.isdecimal())


def stop_charging_daemon():
    service('stop')
    deadline = time.monotonic() + 8
    while charging_daemon_runs(LIVE, STOCK, PROVIDER, LEGACY_PROVIDER):
        if time.monotonic() >= deadline:
            raise RuntimeError('The previous charging daemon is still running.')
        time.sleep(0.1)
    STATE.unlink(missing_ok=True)


def swap_in_launcher():
    if provider_selected():
        if not STOCK.is_file():
            raise RuntimeError('The stock charging daemon backup is missing.')
        return
    if LIVE.is_symlink() or not LIVE.is_file():
        raise RuntimeError('Expected the original charging daemon at /usr/bin/quec_battery.')
    with tempfile.TemporaryDirectory(dir=LIVE.parent) as directory:
        temp = Path(directory)
        backup = temp / 'stock'
        shutil.copy2(LIVE, backup)
        backup.replace(STOCK)
        link = temp / 'live'
        link.symlink_to(LAUNCHER)
        link.replace(LIVE)


def restore_legacy_gate():
    if not LEGACY_RESTORE.exists():
        return
    voltage = load(LEGACY_RESTORE).get('voltage')
    if type(voltage) is not int or not 3000000 <= voltage <= 4400000:
        raise RuntimeError('Invalid voltage saved by the previous charge limit service.')
    if int(read(VREG)) != voltage:
        VREG.write_text(f'{voltage}\n')
        if int(read(VREG)) != voltage:
            raise RuntimeError('The previous charging voltage could not be restored.')
    if read(CHGEN) != '1':
        CHGEN.write_text('1\n')
    LEGACY_RESTORE.unlink()


def restore_stock():
    if provider_selected():
        if not STOCK.is_file():
            raise RuntimeError('The stock charging daemon backup is missing.')
        ENABLED.unlink(missing_ok=True)
        TRIAL.unlink(missing_ok=True)
        stop_charging_daemon()
        STOCK.replace(LIVE)
    if charging_daemon_runs(LIVE, STOCK):
        return
    if service('start'):
        raise RuntimeError('The stock charging service could not be started.')
    deadline = time.monotonic() + 5
    while not charging_daemon_runs(LIVE, STOCK):
        if time.monotonic() >= deadline:
            raise RuntimeError('The stock charging process did not start.')
        time.sleep(0.1)


def activate():
    with locked(DAEMON_LOCK), locked(LOCK):
        if 'e5800' not in read(MODEL).casefold() or not all(
                path.exists() for path in (BAT / 'capacity', ONLINE, PUMP, VREG, CHGEN)):
            raise RuntimeError('The Mudi 7 charging interfaces are unavailable.')
        restore_legacy_gate()
        if not PROVIDER.is_file() or not LAUNCHER.is_file() or not os.access(SERVICE, os.X_OK):
            raise RuntimeError('The charging provider or stock charging service is unavailable.')
        cfg = configuration()
        save(cfg['enabled'], cfg['threshold'])
        selected_before = provider_selected()
        try:
            ENABLED.unlink(missing_ok=True)
            swap_in_launcher()
            stop_charging_daemon()
            TRIAL.touch()
            if service('start'):
                raise RuntimeError('The quec-battery charging daemon could not be started.')
            gauge = gauge_threshold(cfg['threshold']) if cfg['enabled'] else 0
            wait_for_state(gauge, 15, require_valid=True)
            ENABLED.touch()
        except Exception as exc:
            if selected_before or provider_selected():
                try:
                    restore_stock()
                except (OSError, RuntimeError, subprocess.SubprocessError) as rollback:
                    raise RuntimeError(f'{exc} Stock charging recovery failed: {rollback}') from exc
            raise
        finally:
            TRIAL.unlink(missing_ok=True)


def deactivate():
    with locked(LOCK):
        restore_stock()


def main():
    action, *args = sys.argv[1:]
    if action == 'activate' and not args:
        activate()
    elif action == 'deactivate' and not args:
        deactivate()
    else:
        operate(action, *args)


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)
