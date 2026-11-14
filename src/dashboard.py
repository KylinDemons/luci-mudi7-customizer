#!/usr/bin/env python3
"""Interactive dual-city status dashboard for the GL-E5800's built-in screen.

Swipe left/right between settings, active SIM, monitor, OpenClash,
and battery panels. Tap a detail screen's header or swipe right to go back.
Writes RGB565 frames directly to /dev/fb0.

Usage:
  dashboard.py            run the live loop, drawing to /dev/fb0
  dashboard.py --preview  render every screen to PNG files in the given
                          directory (plus a contact sheet), for visual QA
  dashboard.py --calibrate  flash solid red/green/blue full-screen for
                          1s each, to verify the framebuffer colour
                          channel order on real hardware
"""
import json
import math
import os
import re
import signal
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import screen_pin
import screen_i18n
from screen_modules import MODULES, get_modules, selected_index
from vendor_services import VendorServices, empty as empty_vendor, LOGIN_NOTICE
from screen_i18n import tr
from link_probe_config import get_probe_host, host_family
from openclash_config import DEFAULT_RULE_GROUP, get_rule_group

W, H = 240, 320
FB_PATH = "/dev/fb0"
FONT_DIR = "/etc/gl_screen/language/ttf"
CONFIG_DIR = Path("/etc/mudi7-customizer")
CONFIG_FILE = CONFIG_DIR / "config.json"
CHINESE_FONT_DIR = Path("/usr/share/mudi7-customizer/stock-i18n/payload/etc/gl_screen/language/ttf")

BG = (11, 18, 32)
FG = (230, 235, 245)
DIM = (120, 130, 150)
ACCENT = {
    "adguardhome": (110, 200, 130),
    "tailscale": (170, 185, 205),
    "settings": (86, 182, 255),
    "sim": (110, 220, 150),
    "openclash": (200, 140, 255),
    "monitor": (235, 120, 160),
    "battery": (255, 145, 100),
}

DEFAULT_CONFIG = {
    "screen_timeout": 60,
    "screen_brightness": None,
}

_fonts = {}


def font(name, size, latin=False):
    if screen_i18n.chinese() and not latin:
        weight = 'bold' if name == 'default_bold' else 'semibold' if name == 'default_semibold' else 'medium'
        path = str(CHINESE_FONT_DIR / ('default_' + weight + '_zh-cn.ttf'))
    else:
        path = f"{FONT_DIR}/{name}.ttf"
    key = (path, size)
    if key not in _fonts:
        _fonts[key] = ImageFont.truetype(path, size)
    return _fonts[key]


def run_checked(cmd, timeout=8):
    """(ok, stdout). ok is False when the command could not run at all
    (binary missing, timeout, ...) or exited non-zero -- which is a
    different thing from a command that ran fine and printed nothing.
    run() collapses both to "" for the many callers that genuinely do not
    care; anything that would otherwise read a failure as a meaningful
    empty value must use this instead."""
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout, text=True)
        return out.returncode == 0, out.stdout.strip()
    except Exception:
        return False, ""


def run(cmd, timeout=8):
    return run_checked(cmd, timeout)[1]


def ubus_call(obj, method, params=None):
    args = ["ubus", "call", obj, method]
    if params:
        args.append(json.dumps(params))
    out = run(args)
    try:
        return json.loads(out)
    except Exception:
        return {}


def uci_get(key, default=""):
    """`uci -q get` exits non-zero both for an absent key and for uci
    itself failing, so those two stay indistinguishable here -- callers
    that must tell them apart probe the section as
    well. `default` exists so a caller can at least choose which way an
    unreadable value falls."""
    ok, out = run_checked(["uci", "-q", "get", key])
    return out if ok else default


def uci_set(key, val):
    run(["uci", "set", f"{key}={val}"])
    run(["uci", "commit", key.split(".")[0]])


BACKLIGHT_PATH = "/sys/class/backlight/soc:backlight/brightness"

# button_watch.py drops this file instead of switching the UI out from
# under you: a 1-2s hold is easy to trigger by accident (it sits between
# "tap" and the hardware's own poweroff hold), and losing the whole
# dashboard with no warning or undo is a harsh outcome for a misread
# gesture. When the dashboard is running it now gets to ask first. Going
# the other way -- stock UI back to dashboard -- still switches directly,
# since there is nothing of ours on screen to ask with.
SWITCH_REQUEST_FILE = "/var/run/mudi7-customizer/ui-switch-request"
WAKE_REQUEST = Path("/var/run/mudi7-customizer/screen-wake")
SCREEN_POWER = Path(__file__).with_name("screen_sleep.sh")


_asleep_cache = {"ts": 0.0, "val": False}
_ASLEEP_TTL = 0.4


def is_screen_asleep():
    """Cached for _ASLEEP_TTL: mode_live polls this every loop iteration
    (~80x/s), and re-opening a sysfs file that often is pure overhead on
    this SoC. 0.4s is far below any human-perceptible wake latency."""
    now = time.time()
    if now - _asleep_cache["ts"] < _ASLEEP_TTL:
        return _asleep_cache["val"]
    try:
        with open(BACKLIGHT_PATH) as f:
            val = f.read().strip() == "0"
    except Exception:
        val = False
    _asleep_cache["ts"] = now
    _asleep_cache["val"] = val
    return val


POWER_SUPPLY_DIR = "/sys/class/power_supply"
_battery_cache = {"ts": -1e9, "val": None}
_BATTERY_TTL = 15.0


def _read_sys(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return None


def get_battery():
    """(percent, plugged_in) or None when there's no battery to report.

    Read from sysfs, not a ubus object -- the GL-E5800 exposes none for
    this: the fuel gauge is a `type=Battery` supply (cw221X-bat, with
    `capacity`) and the charger a separate `type=USB` supply whose
    `online` is 1 while a cable is in. Matched by type rather than by
    those driver names so a board revision with a different gauge chip
    still works. Cached like is_screen_asleep: draw_header runs on every
    frame of every panel, a battery level doesn't move in 15s."""
    now = time.time()
    if now - _battery_cache["ts"] < _BATTERY_TTL:
        return _battery_cache["val"]
    pct, plugged = None, False
    try:
        for name in os.listdir(POWER_SUPPLY_DIR):
            base = os.path.join(POWER_SUPPLY_DIR, name)
            typ = _read_sys(os.path.join(base, "type"))
            if typ == "Battery" and _read_sys(os.path.join(base, "present")) != "0":
                try:
                    pct = max(0, min(100, int(_read_sys(os.path.join(base, "capacity")))))
                except (TypeError, ValueError):
                    pass
            elif typ in ("USB", "Mains") and _read_sys(os.path.join(base, "online")) == "1":
                plugged = True
    except Exception:
        pass
    val = (pct, plugged) if pct is not None else None
    _battery_cache["ts"] = now
    _battery_cache["val"] = val
    return val


# ---------- config ----------

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            saved = json.loads(CONFIG_FILE.read_text())
            cfg.update((key, saved[key]) for key in DEFAULT_CONFIG if key in saved)
        except Exception:
            pass
    if cfg["screen_timeout"] not in (0, 15, 30, 60, 120, 300, 600):
        cfg["screen_timeout"] = 60
    if type(cfg["screen_brightness"]) is not int or not 10 <= cfg["screen_brightness"] <= 100:
        cfg["screen_brightness"] = None
    return cfg


def save_config(cfg):
    """Atomic: write a sibling temp file, fsync, then rename over the real
    one. A plain write_text can leave a half-written file if the router
    loses power mid-write -- and load_config silently falls back to
    DEFAULT_CONFIG on a parse error, so that would wipe every setting with
    no indication why. There is a Shutdown button in this very UI, so this
    is not a theoretical window."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(cfg, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CONFIG_FILE)


# ---------- background refresh ----------

class Refresher:
    """Runs the periodic data pulls on one background thread and publishes
    the results, so the render loop only ever reads already-fetched
    values.

    Jobs are plain callables run in registration order; a job that raises
    keeps its previous value rather than taking the thread down."""

    def __init__(self):
        self.lock = threading.Lock()
        self._values = {}
        self._jobs = []          # [name, fn, interval, next_at, enabled]
        self._wake = threading.Event()
        # Bumped whenever any value is published, so the render loop can
        # skip re-reading (and re-deriving from) every value on each ~12ms
        # pass when nothing has changed -- most jobs run every 5-30s.
        self.version = 0

    def add(self, name, fn, interval, initial=None, run_now=True, enabled=None):
        with self.lock:
            self._values[name] = initial
        next_at = 0.0 if run_now else time.monotonic() + interval
        self._jobs.append([name, fn, interval, next_at, enabled])

    def get(self, name, default=None):
        with self.lock:
            val = self._values.get(name)
        return default if val is None else val

    def put(self, name, value):
        """Publish a value fetched outside the worker (e.g. the fresh SIM
        state a verified toggle just read), so the render loop's next
        get() can't hand back the older periodic result in its place."""
        with self.lock:
            self._values[name] = value
            self.version += 1

    def request(self, name):
        """Ask for one job to run as soon as the worker next wakes."""
        for job in self._jobs:
            if job[0] == name:
                job[3] = 0.0
        self._wake.set()

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not _stop:
            self._wake.clear()
            deadline = time.monotonic() + 5.0
            for job in self._jobs:
                name, fn, interval, next_at, enabled = job
                if enabled is not None and not enabled():
                    continue
                now = time.monotonic()
                if now >= next_at:
                    job[3] = now + interval
                    try:
                        value = fn()
                    except Exception:
                        value = None
                    if value is not None:
                        with self.lock:
                            self._values[name] = value
                            self.version += 1
                deadline = min(deadline, job[3])
            self._wake.wait(max(0.05, deadline - time.monotonic()))


# ---------- cellular information ----------

def get_sim_status():
    modem = ubus_call("cellular.modem", "status", {"bus": "cpu"})
    slot = modem.get("current_sim_slot")
    networks = ubus_call("cellular.network", "info", {"bus": "cpu"}).get("networks", [])
    active = next((entry for entry in networks if str(entry.get("slot")) == str(slot)), {})
    interface = ubus_call("network.interface.modem_cpu", "status")
    return {
        "slot": str(slot) if slot is not None else None,
        "carrier": active.get("carrier") or None,
        "data_up": bool(interface.get("up")),
    }


# ---------- device settings ----------

# Execute the same GL Lua RPC implementation used by the official web UI.
# Standalone calls provide the OpenResty process and ubus helpers it needs.
_GL_RPC_LUA = r'''
local cjson = require "cjson"
local ubus = require "ubus"
local noop = function() end
ngx = setmetatable({
    log = noop, ERR = 1, WARN = 2, NOTICE = 3, INFO = 4, DEBUG = 5,
    timer = { at = function(_, fn, ...) fn(false, ...) return true end },
    sleep = function(s) os.execute("sleep " .. tonumber(s)) end,
    pipe = { spawn = function(cmd)
        if type(cmd) == "table" then
            local q = {}
            for _, a in ipairs(cmd) do q[#q + 1] = "'" .. tostring(a):gsub("'", "'\\''") .. "'" end
            cmd = table.concat(q, " ")
        end
        local h = io.popen(cmd .. " 2>&1")
        local out = h:read("*a") or ""
        h:close()
        local done = false
        local proc = {
            set_timeouts = noop, shutdown = noop,
            wait = function() return true, "exit", 0 end,
            stdout_read_all = function() if done then return "" end done = true return out end,
            stdout_read_line = function() return nil, "closed" end,
            stderr_read_all = function() return "" end,
            write = function() return 0 end,
            kill = function() return true end,
            pid = function() return 0 end,
        }
        return setmetatable(proc, { __index = function() return noop end })
    end },
}, { __index = function() return noop end })
package.loaded["oui.ubus"] = {
    call = function(object, method, params)
        local conn = ubus.connect()
        local res, err = conn:call(object, method, params or {})
        conn:close()
        return res, err
    end,
    send = function(...) local conn = ubus.connect() conn:send(...) conn:close() end,
    objects = function() local conn = ubus.connect() local o = conn:objects() conn:close() return o end,
}
local m = dofile(arg[1])
local ok, res = pcall(m[arg[2]], cjson.decode(arg[3]))
if not ok then io.stderr:write(tostring(res) .. "\n") os.exit(1) end
io.write("\n", cjson.encode(res or {}), "\n")
'''


def gl_lua_rpc(module_path, method, params, timeout=90):
    """(ok, result) from one of GL's Lua RPC modules (the web UI's own
    backend: /usr/lib/oui-httpd/rpc/<name>), run outside nginx with the
    helpers in _GL_RPC_LUA. ok is False if it couldn't run (module missing
    on another firmware, Lua error) or returned an err_msg."""
    if not os.path.exists(module_path):
        return False, None
    try:
        out = subprocess.run(["lua", "-", module_path, method, json.dumps(params)],
                             input=_GL_RPC_LUA, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return False, None
    if out.returncode != 0:
        return False, None
    try:
        res = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:
        res = {}
    if isinstance(res, dict) and (res.get("err_msg") or res.get("err_code", 0) != 0):
        return False, res
    return True, res


_GL_NETWORK_RPC = "/usr/lib/oui-httpd/rpc/network"
ACCELERATION_MODES = {0: "Auto", 1: "Hardware", 2: "Software"}
_acceleration_lock = threading.RLock()
ACCELERATION_CONFLICT = "OpenClash is enabled. Turn off OpenClash before enabling network acceleration."
OPENCLASH_CONFLICT = "Network acceleration is enabled. Turn it off before starting OpenClash."


def acceleration_empty(message="Unavailable"):
    return {"available": False, "enable": False, "actype": None, "list": [], "error": message}


def get_network_acceleration():
    with _acceleration_lock:
        ok, result = gl_lua_rpc(_GL_NETWORK_RPC, "get_netnat_config", {}, timeout=8)
        if not ok or not isinstance(result, dict):
            return acceleration_empty("Could not read GL settings")
        modes = result.get("list")
        if not isinstance(result.get("enable"), bool) or not isinstance(modes, list):
            return acceleration_empty("Unsupported GL response")
        modes = [mode for mode in modes if type(mode) is int and mode in ACCELERATION_MODES]
        mode = result.get("actype")
        if type(mode) is not int or mode not in modes:
            return acceleration_empty("Unsupported acceleration mode")
        return dict(result, available=True, error="", list=modes)


def set_network_acceleration(enabled=None, mode=None):
    with _acceleration_lock:
        # Always re-read GL's state so a toggle retains the mode chosen in the web UI.
        current = get_network_acceleration()
        if not current["available"]:
            return current, current["error"]
        if mode is not None:
            if not current["enable"]:
                return current, "Enable acceleration to change mode"
            if mode not in current["list"]:
                return current, "Mode is unavailable"
        target = {"enable": not current["enable"] if enabled is None and mode is None else current["enable"],
                  "actype": current["actype"] if mode is None else mode}
        if enabled is not None:
            target["enable"] = enabled
        if target["enable"]:
            clash = get_openclash_status()
            if clash["enabled"] or clash["running"] or clash["busy"]:
                return current, ACCELERATION_CONFLICT
        ok, result = gl_lua_rpc(_GL_NETWORK_RPC, "set_netnat_config", target, timeout=90)
        fresh = get_network_acceleration()
        if not ok:
            detail = result.get("err_msg", "") if isinstance(result, dict) else ""
            return fresh, detail or "GL rejected the change"
        if not fresh["available"] or any(fresh[key] != value for key, value in target.items()):
            return fresh, "Could not verify GL settings"
        return fresh, None


_lan_ip_cache = {"ts": 0.0, "val": None}


def get_system_info():
    """Reads /proc/uptime directly instead of spawning `cat`, and caches
    the LAN IP for a minute: mode_live calls this every 2s purely for the
    uptime line, and two process spawns every 2s is real load on this
    SoC for a number that changes once a minute."""
    uptime_min = 0
    try:
        with open("/proc/uptime") as f:
            uptime_min = int(float(f.read().split()[0]) / 60)
    except Exception:
        pass
    now = time.time()
    if _lan_ip_cache["val"] is None or now - _lan_ip_cache["ts"] > 60:
        _lan_ip_cache["val"] = uci_get("network.lan.ipaddr") or "192.168.8.1"
        _lan_ip_cache["ts"] = now
    return {"uptime_min": uptime_min, "lan_ip": _lan_ip_cache["val"]}


def reboot_router():
    subprocess.Popen(["/sbin/reboot"])


def shutdown_router():
    subprocess.Popen(["/sbin/poweroff"])


# Longest the "Shutting down…/Restarting…" screen keeps spinning while
# waiting for the system to actually start taking services down (which is
# when procd's TERM reaches this process). If that never happens the
# command didn't take, and the UI comes back instead of spinning forever.
POWER_SCREEN_MAX_S = 180.0


def panel_power(kind, phase_deg=0.0, elapsed=None, final=False):
    """Full-screen feedback for Reboot / Shutdown. Going straight back to
    the Settings page after /sbin/reboot or /sbin/poweroff (the old
    behaviour) left the screen sitting there, untouched, for the ~10-30s
    the system takes to stop -- it looked frozen. `final` is the last
    frame, drawn once this process is being stopped: it stays on the
    panel through the rest of the shutdown."""
    img, d = new_canvas()
    reboot = kind == "reboot"
    accent = ACCENT["settings"] if reboot else (220, 95, 95)
    cx, cy = W // 2, 132
    if final:
        # static ring, no motion: nothing will redraw it again
        d.ellipse([cx - 28, cy - 28, cx + 28, cy + 28], outline=_mix(BG, accent, 0.5), width=4)
        if reboot:
            _draw_arrow_circle(d, cx, cy, accent)
        else:
            _draw_power_glyph(d, cx, cy, accent)
        title = tr("Restarting…") if reboot else tr("Powering off")
        sub = tr("Back in about a minute") if reboot else tr("The screen will go dark")
    else:
        draw_ring_spinner(d, cx, cy, 28, phase_deg, accent, width=4)
        title = tr("Restarting…") if reboot else tr("Shutting down…")
        sub = tr("Please wait") + (f" · {int(elapsed)}s" if elapsed is not None else "")
    centered_text(d, W / 2, cy + 48, title, font("default_bold", 20), FG)
    centered_text(d, W / 2, cy + 76, sub, font("default_medium", 13), DIM)
    if not reboot:
        centered_text(d, W / 2, cy + 96, tr("Don't unplug until it's off"), font("default_medium", 11), DIM)
    return img


def _draw_power_glyph(d, cx, cy, color):
    r = 12
    d.arc([cx - r, cy - r, cx + r, cy + r], start=300, end=240, fill=color, width=3)
    d.line([cx, cy - r - 3, cx, cy - 2], fill=color, width=3)


def _draw_arrow_circle(d, cx, cy, color):
    r = 12
    d.arc([cx - r, cy - r, cx + r, cy + r], start=320, end=260, fill=color, width=3)
    tip = (cx + r * math.cos(math.radians(320)), cy + r * math.sin(math.radians(320)))
    d.polygon([(tip[0] + 5, tip[1] - 2), (tip[0] - 4, tip[1] - 5), (tip[0] - 1, tip[1] + 5)], fill=color)


def switch_to_stock_ui():
    # Non-blocking AND detached: toggle.sh off stops mudi7-dashboard (this very
    # process), so it has to outlive us. Popen alone leaves it in our
    # process group, so procd's stop can take it down partway through --
    # after gl_screen has been stopped but before it is started again,
    # which leaves the physical screen dark with neither UI running.
    # start_new_session puts it in its own session so that can't happen.
    try:
        subprocess.Popen(["/usr/libexec/mudi7-customizer/toggle.sh", "off"], start_new_session=True)
    except TypeError:      # very old Python without start_new_session
        subprocess.Popen(["setsid", "/usr/libexec/mudi7-customizer/toggle.sh", "off"])


# ---------- system monitor (bandwidth + CPU/RAM/temp) ----------

def get_wan_iface():
    """Pick the interface holding the lowest-metric default route. This
    device dual-WANs (repeater WiFi uplink, usually wlan4, vs. the cellular
    modem rmnet_data0 as failover) so the active WAN interface isn't fixed --
    br-lan is just the local LAN bridge and stays near-zero unless another
    device is actively using this router's own AP, which made the old
    hardcoded br-lan reading look permanently decorative.

    Cached for 2s: mode_live's idle branch reaches this through
    get_wan_conn_type on every ~12ms loop pass (911 reads of
    /proc/net/route in a 25s profile), for a value that changes on a WAN
    failover, not per frame."""
    now = time.time()
    if now - _wan_iface_cache["ts"] < _WAN_IFACE_TTL:
        return _wan_iface_cache["val"]
    best_iface, best_metric = None, None
    try:
        with open("/proc/net/route") as f:
            next(f)
            for line in f:
                parts = line.split()
                if len(parts) < 7 or parts[1] != "00000000":
                    continue
                metric = int(parts[6])
                if best_metric is None or metric < best_metric:
                    best_iface, best_metric = parts[0], metric
    except Exception:
        pass
    _wan_iface_cache["ts"] = now
    _wan_iface_cache["val"] = best_iface
    return best_iface


_wan_iface_cache = {"ts": -1e9, "val": None}
_WAN_IFACE_TTL = 2.0


_CELL_NR_TYPES = {5, 51}
_CELL_LTE_TYPES = {4, 41}
_CELL_INVALID = -32768


def _cell_int(v):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    return None if v == _CELL_INVALID else v


def _parse_cell_signal(raw):
    """Normalises firmware 4.10's `cellular.network cell_info` reply --
    a `signal` list with one entry per aggregated carrier, `ca` 0 being
    the primary -- into the dict shape the rest of this file reads:
    `mode` (non-empty only while registered), primary `rsrp`/`strength`,
    and `carriers` for the band/CA display."""
    carriers = []
    for s in sorted(raw.get("signal") or [], key=lambda s: _cell_int(s.get("ca")) or 0):
        band = _cell_int(s.get("band"))
        ntype = _cell_int(s.get("network_type"))
        # In airplane mode (or with no service) cell_info isn't empty: it
        # keeps one placeholder entry with band 0, network_type 0 and
        # rsrp -32768 -- confirmed live. Counting that as a carrier ("B0")
        # made the modem look registered, kept the header's signal bars
        # up and made airplane mode look like it never took.
        if not band or not ntype:
            continue
        nr = ntype in _CELL_NR_TYPES
        try:
            mhz = float(s.get("bandwidth"))
        except (TypeError, ValueError):
            mhz = None
        carriers.append({
            "band": f"n{band}" if nr else f"B{band}",
            "nr": nr,
            "lte": ntype in _CELL_LTE_TYPES,
            "mhz": mhz if mhz and mhz > 0 else None,
            "rsrp": _cell_int(s.get("rsrp")),
            "strength": _cell_int(s.get("strength")),
            "rsrq": _cell_int(s.get("rsrq")),
            "sinr": _cell_int(s.get("sinr")),
            "rssi": _cell_int(s.get("rssi")),
            "pci": _cell_int(s.get("pci")),
            "earfcn": _cell_int(s.get("earfcn")),
        })
    if not carriers:
        return {}
    n_nr = sum(1 for c in carriers if c["nr"])
    n_lte = sum(1 for c in carriers if c["lte"])
    if n_nr and n_lte:
        mode = "NR5G-NSA"
    elif n_nr:
        mode = "NR5G-SA"
    elif n_lte:
        mode = "LTE"
    else:
        mode = {1: "GSM", 2: "WCDMA", 3: "WCDMA"}.get(_cell_int(raw.get("network_type")), "Cellular")
    primary = carriers[0]
    return {"mode": mode, "rsrp": primary["rsrp"], "strength": primary["strength"],
            "carriers": carriers, "tac": raw.get("tac"), "cell_id": raw.get("cell_id")}


def _get_active_cell_info(slot=None):
    """Serving-cell metrics for the active slot. Fall back to the old
    firmware's network info response when cell_info is unavailable."""
    if slot is None:
        modem = ubus_call("cellular.modem", "status", {"bus": "cpu"})
        slot = modem.get("current_sim_slot", 1)
    try:
        slot = int(slot)
    except (TypeError, ValueError):
        slot = 1
    raw = ubus_call("cellular.network", "cell_info", {"bus": "cpu", "slot": slot})
    if "signal" in raw:
        return _parse_cell_signal(raw)
    net = ubus_call("cellular.network", "info", {"bus": "cpu", "slot": slot})
    for n in net.get("networks", []):
        cell = n.get("cell_info") or {}
        if cell.get("mode"):
            return cell
    return {}


def cell_network_label(cell):
    """(headline, detail) for the SIM page's network-type readout, e.g.
    ("5G", "NSA"), ("4G+", "LTE-A"), ("4G", "LTE") -- the same 4G / 4G+ /
    5G vocabulary a phone status bar uses. None when not registered."""
    mode = (cell or {}).get("mode", "")
    if not mode:
        return None
    mode_u = mode.upper()
    carriers = cell.get("carriers") or []
    if "NR" in mode_u:
        return "5G", "SA" if "SA" in mode_u and "NSA" not in mode_u else "NSA"
    if "LTE" in mode_u:
        if sum(1 for c in carriers if c.get("lte")) > 1:
            return "4G+", "LTE-A"
        return "4G", "LTE"
    if "WCDMA" in mode_u or "UMTS" in mode_u:
        return "3G", "WCDMA"
    if "GSM" in mode_u:
        return "2G", "GSM"
    return "4G", mode


def get_wan_conn_type(cell_info=None):
    """Short label for the header's connection-status indicator: Repeater
    (WiFi client uplink), Ethernet, or 4G/5G (cellular, radio access tech
    read from cellular.network's cell_info.mode, e.g. "LTE FDD" -> 4G,
    anything with "NR" -> 5G). The live loop refreshes this periodically
    outside the panel render functions."""
    iface = get_wan_iface()
    if not iface:
        return None
    if iface.startswith("wlan"):
        return "Repeater"
    if iface.startswith("eth"):
        return "Ethernet"
    if "rmnet" in iface or "modem" in iface:
        label = cell_network_label(cell_info if cell_info is not None else _get_active_cell_info())
        return label[0] if label else "Cellular"
    return None


def get_cell_signal(cell_info=None):
    """(bars 0-4, "4G"/"4G+"/"5G") for the active SIM's current cellular
    signal -- the modem's own level where it reports one, else derived
    from RSRP (dBm); None if not registered/no signal at all --
    matches ordinary phone status-bar behavior of hiding the cellular
    indicator entirely when there's nothing to show. Independent of
    whether cellular is actually the active WAN (get_wan_conn_type) --
    this reflects the modem's own registration/signal, the same way a
    phone shows signal bars regardless of whether you're on WiFi."""
    cell = cell_info if cell_info is not None else _get_active_cell_info()
    label = cell_network_label(cell)
    if not label:
        return None
    rat = label[0]
    # The modem's own 0-4 level (4.10's cell_info) is what the stock UI
    # draws, so prefer it over re-deriving bars from RSRP here.
    strength = cell.get("strength")
    if isinstance(strength, int) and 0 <= strength <= 4:
        return strength, rat
    try:
        rsrp = int(cell.get("rsrp"))
    except (TypeError, ValueError):
        return None
    if rsrp >= -80:
        bars = 4
    elif rsrp >= -95:
        bars = 3
    elif rsrp >= -105:
        bars = 2
    elif rsrp >= -115:
        bars = 1
    else:
        bars = 0
    return bars, rat


LINK_PROBE_INTERVAL = 3.0
LINK_PROBE_WINDOW = 20


def sample_link_health(previous):
    iface = get_wan_iface()
    # Read per probe so a target changed in LuCI applies without restarting
    # the dashboard. A changed target also clears the history: latencies
    # against a different host are not comparable. This only ever runs while
    # the Monitor panel is on screen -- the refresher job is gated on
    # link_session, so leaving the panel stops the probes.
    host = get_probe_host()
    same_probe = previous and previous["iface"] == iface and previous.get("host") == host
    history = previous["history"] if same_probe else ()
    sample = {"iface": iface, "host": host, "history": history, "status": "No WAN"}
    if not iface:
        return sample

    sample["status"] = "Unavailable"
    # An IPv6 literal is probed over IPv6; a name keeps the historical IPv4
    # probe (busybox ping would default to IPv4 anyway).
    family = "-6" if host_family(host) == "ipv6" else "-4"
    try:
        result = subprocess.run(
            ["ping", family, "-n", "-I", iface, "-c", "1", "-W", "1", host],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return sample

    if get_wan_iface() != iface:
        return None  # Discard a probe that overlapped a WAN switch.

    latency = re.search(r"\btime[=<]([0-9]+(?:\.[0-9]+)?)\s*ms", result.stdout)
    if result.returncode == 0 and latency:
        rtt = float(latency.group(1))
        sample["status"] = "Reachable"
    elif re.search(r"1 packets? transmitted, 0 (?:packets? )?received", result.stdout):
        rtt = None
        sample["status"] = "No reply"
    else:
        return sample

    sample["history"] = (*history, rtt)[-LINK_PROBE_WINDOW:]
    return sample


def sample_cpu(prev):
    """prev: (idle, total) or None, from /proc/stat's aggregate 'cpu' line.
    Returns (new_sample, cpu_pct). cpu_pct is None until there's a previous
    sample (needs a delta, not just a point-in-time read)."""
    try:
        with open("/proc/stat") as f:
            fields = [int(x) for x in f.readline().split()[1:]]
        idle = fields[3] + fields[4]
        total = sum(fields)
    except Exception:
        return prev, None
    if prev is None:
        return (idle, total), None
    pidle, ptotal = prev
    dtotal = total - ptotal
    if dtotal <= 0:
        return (idle, total), None
    pct = max(0.0, min(100.0, 100 * (1 - (idle - pidle) / dtotal)))
    return (idle, total), pct


def get_ram_stats():
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.strip().split()[0])
        total_kb = info.get("MemTotal", 0)
        avail_kb = info.get("MemAvailable", 0)
        if not total_kb:
            return None, None, None
        used_kb = total_kb - avail_kb
        return 100 * used_kb / total_kb, used_kb / 1024 / 1024, total_kb / 1024 / 1024
    except Exception:
        return None, None, None


_TEMP_ZONE_PATH = None
_TEMP_ZONE_PREFERRED = ["cpuss-0", "aoss-0", "sys-therm-1"]


def get_temp_c():
    """thermal_zone0 on this hardware ('sdr0') is an unpowered sensor that
    always reports the sentinel -273000 (absolute zero); pick a real sensor
    by name instead of assuming zone index 0, and cache the path found."""
    global _TEMP_ZONE_PATH
    if _TEMP_ZONE_PATH is None:
        base = "/sys/class/thermal"
        candidates = {}
        try:
            for name in os.listdir(base):
                if not name.startswith("thermal_zone"):
                    continue
                try:
                    with open(f"{base}/{name}/type") as f:
                        ztype = f.read().strip()
                    with open(f"{base}/{name}/temp") as f:
                        raw = int(f.read().strip())
                except Exception:
                    continue
                if -50000 < raw < 150000:
                    candidates[ztype] = f"{base}/{name}/temp"
        except Exception:
            pass
        for name in _TEMP_ZONE_PREFERRED:
            if name in candidates:
                _TEMP_ZONE_PATH = candidates[name]
                break
        else:
            _TEMP_ZONE_PATH = next(iter(candidates.values()), False)
    if not _TEMP_ZONE_PATH:
        return None
    try:
        with open(_TEMP_ZONE_PATH) as f:
            return int(f.read().strip()) / 1000
    except Exception:
        return None


# ---------- speed test ----------

# Cloudflare's speed-test endpoints (the ones speed.cloudflare.com itself
# uses): __down streams N bytes, __up accepts a POST body and discards it
# -- chunked uploads included, confirmed from this router with curl -T -.
SPEEDTEST_DOWN_URL = "https://speed.cloudflare.com/__down?bytes={}"
SPEEDTEST_UP_URL = "https://speed.cloudflare.com/__up"
SPEEDTEST_STREAMS = 4           # parallel connections per direction
SPEEDTEST_PHASE_S = 6.0         # length of each of download / upload
SPEEDTEST_RAMP_S = 1.0          # excluded from the result: TCP slow start
SPEEDTEST_DOWN_CAP = 250 * 10**6   # hard stop, bytes -- this is mobile data
SPEEDTEST_UP_CAP = 80 * 10**6
SPEEDTEST_CHUNK = 256 * 1024


class SpeedTest:
    """Download then upload over SPEEDTEST_STREAMS parallel curl processes,
    run on its own thread so the screen keeps animating.

    Throughput is counted from the bytes these streams themselves move
    (piped through this process), NOT from the WAN interface's counters:
    those include every LAN client's traffic too -- measured live, a 5 MB
    test download moved the rmnet_data0 counter by 6.5 MB. curl does the
    TLS; Python only counts and discards, so it's never the bottleneck.

    The result is the average over the phase minus its first
    SPEEDTEST_RAMP_S. For upload the count is of bytes handed to curl, so
    pipe/socket buffering makes it run ahead at the start -- the same
    ramp cut is what keeps that out of the number."""

    def __init__(self):
        self.lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread = None
        self._reset()

    def _reset(self):
        self.phase = "idle"         # idle | download | upload | done | error | cancelled
        self.live_mbps = 0.0
        self.progress = 0.0         # 0..1 through the current phase
        self.down_mbps = None
        self.up_mbps = None
        self.bytes_used = 0
        self.error = None
        self.via = None

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.running:
            return
        with self.lock:
            self._reset()
            self.phase = "download"
            self.via = get_wan_conn_type() or get_wan_iface()
        self._cancel.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def cancel(self):
        if self.running:
            self._cancel.set()

    def snapshot(self):
        with self.lock:
            return {"phase": self.phase, "live": self.live_mbps, "progress": self.progress,
                    "down": self.down_mbps, "up": self.up_mbps, "bytes": self.bytes_used,
                    "error": self.error, "via": self.via}

    def _run(self):
        try:
            down = self._phase("download")
            with self.lock:
                self.down_mbps = down
            if self._cancel.is_set() or _stop:
                raise _SpeedTestCancelled()
            with self.lock:
                self.phase, self.live_mbps, self.progress = "upload", 0.0, 0.0
            up = self._phase("upload")
            with self.lock:
                self.up_mbps = up
                self.phase, self.live_mbps = "done", 0.0
        except _SpeedTestCancelled:
            with self.lock:
                self.phase, self.live_mbps = "cancelled", 0.0
        except Exception as e:
            with self.lock:
                self.phase, self.live_mbps, self.error = "error", 0.0, str(e) or "Test failed"

    def _phase(self, kind):
        download = kind == "download"
        cap = SPEEDTEST_DOWN_CAP if download else SPEEDTEST_UP_CAP
        count = [0]
        count_lock = threading.Lock()
        stop = threading.Event()
        procs = []

        def add(n):
            with count_lock:
                count[0] += n

        def reader(p):
            try:
                while not stop.is_set():
                    chunk = p.stdout.read(SPEEDTEST_CHUNK)
                    if not chunk:
                        break
                    add(len(chunk))
            except Exception:
                pass

        payload = os.urandom(SPEEDTEST_CHUNK)   # incompressible

        def writer(p):
            try:
                while not stop.is_set():
                    add(p.stdin.write(payload) or 0)   # raw pipe: may be partial
            except Exception:
                pass                             # curl killed / pipe closed

        max_time = str(int(SPEEDTEST_PHASE_S + 6))
        per_stream = cap // SPEEDTEST_STREAMS + 1
        for _ in range(SPEEDTEST_STREAMS):
            if download:
                cmd = ["curl", "-s", "--max-time", max_time, "-o", "-",
                       SPEEDTEST_DOWN_URL.format(per_stream)]
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
                target = reader
            else:
                cmd = ["curl", "-s", "--max-time", max_time, "-o", "/dev/null", "-X", "POST",
                       "-T", "-", "-H", "Content-Type: application/octet-stream", SPEEDTEST_UP_URL]
                p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, bufsize=0)
                target = writer
            procs.append(p)
            threading.Thread(target=target, args=(p,), daemon=True).start()

        t0 = time.time()
        samples = [(t0, 0)]
        ramp = None
        try:
            while True:
                time.sleep(0.2)
                now = time.time()
                with count_lock:
                    total = count[0]
                samples.append((now, total))
                elapsed = now - t0
                if ramp is None and elapsed >= SPEEDTEST_RAMP_S:
                    ramp = (now, total)
                # live figure: rate over roughly the last second
                past = next((s for s in samples if s[0] >= now - 1.0), samples[0])
                live = (total - past[1]) * 8 / max(0.05, now - past[0]) / 1e6
                with self.lock:
                    self.live_mbps = live
                    self.progress = min(1.0, elapsed / SPEEDTEST_PHASE_S)
                if (elapsed >= SPEEDTEST_PHASE_S or total >= cap or self._cancel.is_set() or _stop
                        or all(p.poll() is not None for p in procs)):
                    break
        finally:
            stop.set()
            for p in procs:
                try:
                    p.kill()
                except Exception:
                    pass
            for p in procs:
                try:
                    p.wait(timeout=2)
                except Exception:
                    pass
            with count_lock:
                total = count[0]
            with self.lock:
                self.bytes_used += total

        if self._cancel.is_set() or _stop:
            raise _SpeedTestCancelled()
        end_t, end_b = samples[-1]
        if end_b == 0:
            raise RuntimeError("No connection to the test server")
        start_t, start_b = ramp if ramp and end_t - ramp[0] >= 0.5 else samples[0]
        return (end_b - start_b) * 8 / max(0.05, end_t - start_t) / 1e6


class _SpeedTestCancelled(Exception):
    pass


def openclash_installed():
    return os.path.exists("/etc/init.d/openclash")


def openclash_status_empty(installed=False):
    return {"installed": installed, "enabled": False, "running": False, "busy": False, "mode": "rule"}


def get_openclash_status():
    """`enabled` is only what uci says OpenClash *should* be doing; the
    toggle shows `running` -- the clash core process actually existing,
    under OpenClash's `clash` name or the `mihomo` name. `busy` is the
    init script still mid-start/stop (LuCI's is_start()). Showing uci's
    flag was why the toggle could read "on" with nothing running (a start
    that failed) or disagree with the node/traffic rows below it."""
    if not openclash_installed():
        return openclash_status_empty()
    return {
        "installed": True,
        "enabled": uci_get("openclash.config.enable") == "1",
        "running": run_checked(["pidof", "clash", "mihomo"])[0],
        "busy": run_checked(["pgrep", "-f", "/etc/init.d/openclash"])[0],
        "mode": uci_get("openclash.config.proxy_mode") or "rule",
    }


def openclash_api_ok():
    base, headers = _mihomo_api()
    return run_checked(["curl", "-s", "-o", "/dev/null", "-m", "2"] + headers + [f"{base}/version"])[0]


def openclash_start_error():
    acceleration = get_network_acceleration()
    if not acceleration["available"]:
        return "Could not check network acceleration. Try again."
    return OPENCLASH_CONFLICT if acceleration["enable"] else None


def set_openclash_enabled(enabled):
    with _acceleration_lock:
        if not openclash_installed():
            return "OpenClash is not installed"
        if enabled:
            error = openclash_start_error()
            if error:
                return error
        uci_set("openclash.config.enable", "1" if enabled else "0")
        subprocess.Popen(["/etc/init.d/openclash", "start" if enabled else "stop"])
        return None


OPENCLASH_START_TIMEOUT = 120.0
OPENCLASH_STOP_TIMEOUT = 60.0


def openclash_fully(want_on):
    """True once OpenClash has *finished* getting to the wanted state.
    Timed live: a start has the core up at ~5s and the API answering at
    ~6s, but the init script keeps going (firewall/DNS setup) until ~11s;
    a stop takes ~6s. So "on" = core up + API answering + init done, and
    "off" = core gone + init done -- not just the first sign of life."""
    st = get_openclash_status()
    if st["busy"]:
        return False
    if want_on:
        return st["running"] and openclash_api_ok()
    return not st["running"]


def wait_for_openclash_state(want_on, timeout=None, poll=1.0):
    """Blocks (run it under run_with_spinner) until openclash_fully(want_on)
    or timeout; returns whether it got there. Generous timeouts on
    purpose: a start can also fetch subscriptions and rebuild rules, and
    reporting failure early while it's still coming up is exactly the
    "shows off while actually on" confusion this replaced."""
    timeout = timeout or (OPENCLASH_START_TIMEOUT if want_on else OPENCLASH_STOP_TIMEOUT)
    deadline = time.time() + timeout
    while time.time() < deadline and not _stop:
        if openclash_fully(want_on):
            return True
        time.sleep(poll)
    return openclash_fully(want_on)


def set_openclash_mode(mode):
    with _acceleration_lock:
        if not openclash_installed():
            return "OpenClash is not installed"
        restart = uci_get("openclash.config.enable") == "1"
        if restart:
            error = openclash_start_error()
            if error:
                return error
        uci_set("openclash.config.proxy_mode", mode)
        if restart:
            subprocess.Popen(["/etc/init.d/openclash", "restart"])
        return None


def _mihomo_api():
    port = uci_get("openclash.config.cn_port") or "9090"
    password = uci_get("openclash.config.dashboard_password")
    headers = ["-H", f"Authorization: Bearer {password}"] if password else []
    return f"http://127.0.0.1:{port}", headers


def openclash_selector(mode, traf):
    if mode == "global":
        return "GLOBAL", "Global Node"
    group = traf["rule_group"]
    return group, "Default Proxy" if group == DEFAULT_RULE_GROUP else "Rule Group"


def openclash_traffic_empty(rule_group=None):
    return {"running": False, "up_mb": None, "down_mb": None, "groups": {},
            "rule_group": rule_group if rule_group is not None else get_rule_group()}


def get_openclash_traffic_and_node():
    rule_group = get_rule_group()
    if not openclash_installed():
        return openclash_traffic_empty(rule_group)
    base, headers = _mihomo_api()
    conn_raw = run(["curl", "-s", "--max-time", "2"] + headers + [f"{base}/connections"])
    try:
        conn = json.loads(conn_raw)
        up_mb = conn.get("uploadTotal", 0) / 1024 / 1024
        down_mb = conn.get("downloadTotal", 0) / 1024 / 1024
    except Exception:
        return openclash_traffic_empty(rule_group)

    groups = {}
    proxies_raw = run(["curl", "-s", "--max-time", "2"] + headers + [f"{base}/proxies"])
    try:
        proxies = json.loads(proxies_raw).get("proxies", {})
    except (ValueError, AttributeError):
        proxies = {}
    if isinstance(proxies, dict):
        for name in (rule_group, "GLOBAL"):
            info = proxies.get(name)
            if isinstance(info, dict) and info.get("type") == "Selector":
                options = info.get("all")
                current = info.get("now")
                groups[name] = {
                    "now": current if isinstance(current, str) else None,
                    "all": [item for item in options if isinstance(item, str)]
                    if isinstance(options, list) else [],
                }
    return {"running": True, "up_mb": up_mb, "down_mb": down_mb, "groups": groups, "rule_group": rule_group}


def select_openclash_node(group, name):
    base, headers = _mihomo_api()
    ok, status = run_checked(["curl", "-s", "--max-time", "3", "-o", "/dev/null",
                              "-w", "%{http_code}", "-X", "PUT"] + headers +
                             ["-H", "Content-Type: application/json",
                              "-d", json.dumps({"name": name}),
                              f"{base}/proxies/{quote(group, safe='')}"])
    return ok and status.startswith("2")


def _draw_aa(d, x0, y0, w, h, color, draw_fn, scale=4):
    """Anti-aliased shapes: draw_fn(mask_draw, scale) paints white onto a
    `scale`x supersampled mask covering (x0, y0, w, h), which is then
    downsampled and used to paste `color`. PIL's own arcs/ellipses are
    aliased, which at icon sizes turns thin parallel strokes into one
    stair-stepped blob."""
    x0, y0 = int(round(x0)), int(round(y0))
    mask = Image.new("L", (w * scale, h * scale), 0)
    draw_fn(ImageDraw.Draw(mask), scale)
    mask = mask.resize((w, h), Image.LANCZOS)
    d._image.paste(color, (x0, y0, x0 + w, y0 + h), mask)


# ---------- loading overlay ----------

SPINNER_SPEED_DPS = 300.0     # degrees/second -- iOS-ish, brisk but calm

# Upper bound for the "Switching to stock UI…" transition animation.
# toggle.sh's own worst case (wait_gone: six 0.2s polls, then SIGKILL any
# survivor) is ~1.2s, so this leaves real slack for gl_screen's own
# startup while still being short enough that the overlay itself never
# becomes the thing that looks stuck.
SWITCH_OVERLAY_MAX_S = 3.0


def draw_ring_spinner(d, cx, cy, r, phase_deg, accent, width=3, arc_deg=270, segments=18):
    """A rotating arc with a brightness ramp toward its leading end. PIL
    can only stroke an arc in one flat colour, so the ramp is drawn as
    `segments` short arcs -- cheap at this size and it reads as a proper
    fading tail rather than a plain spinning stick."""
    track = _mix(BG, accent, 0.18)
    d.arc([cx - r, cy - r, cx + r, cy + r], 0, 360, fill=track, width=width)
    step = arc_deg / segments
    for i in range(segments):
        t = (i + 1) / segments                     # 0 = tail, 1 = head
        col = _mix(track, accent, t * t)           # squared: longer dim tail
        a0 = phase_deg + i * step
        d.arc([cx - r, cy - r, cx + r, cy + r], a0, a0 + step + 1, fill=col, width=width)


def draw_loading_overlay(base, label=None, phase_deg=0.0, accent=None):
    """Dim whatever was on screen and float a spinner (and optional
    caption) over it. Keeping the real screen visible underneath -- rather
    than replacing it with a blank "loading" page -- is what makes a slow
    action read as *busy* instead of *crashed*, which was the whole
    complaint: several actions here take seconds with no feedback at all."""
    accent = accent or ACCENT["settings"]
    img = Image.blend(base, Image.new("RGB", (W, H), (0, 0, 0)), 0.62)
    d = ImageDraw.Draw(img)
    cx, cy = W // 2, H // 2 - (12 if label else 0)
    label = tr(label)
    if label:
        f = font("default_medium", 13)
        tw = d.textlength(label, font=f)
        card_w = max(132, tw + 44)
        card_h = 116
        x0, y0 = cx - card_w / 2, cy - 44
        d.rounded_rectangle([x0, y0, x0 + card_w, y0 + card_h], radius=16,
                            fill=(26, 34, 52), outline=(48, 58, 82))
        draw_ring_spinner(d, cx, cy + 2, 17, phase_deg, accent)
        centered_text(d, cx, y0 + card_h - 28, label, f, FG)
    else:
        draw_ring_spinner(d, cx, cy, 19, phase_deg, accent)
    return img


def run_with_spinner(base_img, label, fn, accent=None, min_visible=0.4, fps=30):
    """Run fn() on a worker thread, animating the overlay until it returns.

    Use this for actions that wait for a result, such as applying a data
    cap or switching SIM. The spinner overlays the screen that opened it. min_visible stops a fast action from flashing the
    overlay for two frames. `label` may be a zero-arg callable, re-read
    every frame, for actions whose caption should change as they go
    (e.g. "Applying…" -> "Verifying… 12s")."""
    box = {}

    def worker():
        try:
            box["value"] = fn()
        except Exception:
            box["value"] = None

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    t0 = time.time()
    interval = 1.0 / fps
    while True:
        elapsed = time.time() - t0
        if not th.is_alive() and elapsed >= min_visible:
            break
        text = label() if callable(label) else label
        write_frame(draw_loading_overlay(base_img, text, elapsed * SPINNER_SPEED_DPS, accent))
        time.sleep(interval)
    # Anything tapped while the overlay was up was aimed at the busy
    # screen, not at whatever comes next -- drop it rather than letting it
    # land on a screen the user never saw.
    with touch_state.lock:
        touch_state.release_pending = False
    return box.get("value")


# ---------- widgets ----------

def new_canvas():
    img = Image.new("RGB", (W, H), BG)
    return img, ImageDraw.Draw(img)


def draw_page_dots(d, active_idx, count=None):
    """Dot centres are 16px apart, so the row spans (count - 1) * 16
    between the first and last centre -- centring on that, not on
    count * 16, which put the whole row 8px left of the screen's middle."""
    if count is None:
        count = len(PANEL_NAMES)
    step = 16
    x0 = W / 2 - (count - 1) * step / 2
    y = H - 12
    for i in range(count):
        x = x0 + i * step
        r = 4 if i == active_idx else 3
        color = FG if i == active_idx else DIM
        d.ellipse([x - r, y - r, x + r, y + r], fill=color)


def _mix(c1, c2, t):
    return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))


def draw_signal_bars(d, x0, y_base, bars, color, dim_color, bar_w=3, gap=2, max_h=10):
    """4 ascending bars (classic phone signal icon), bottom-aligned at
    y_base, growing upward. `bars` (0-4) of them filled with `color`, the
    rest drawn dim (still visible against the colored header, just muted)."""
    for i in range(4):
        bh = max_h * (i + 1) / 4
        bx0 = x0 + i * (bar_w + gap)
        fill = color if i < bars else dim_color
        d.rectangle([bx0, y_base - bh, bx0 + bar_w, y_base], fill=fill)


BATTERY_BODY_W, BATTERY_BODY_H, BATTERY_NUB_W = 27, 15, 3
BATTERY_ICON_W = BATTERY_BODY_W + BATTERY_NUB_W


def _draw_battery_icon(d, x1, cy, pct, ink, paper, low_color):
    """Phone-style battery with the percentage written *inside* it, right
    edge at x1, vertically centred on cy. `ink` is the header's text
    colour, `paper` the header's own colour.

    The number is two-tone: where it sits over the filled part it's drawn
    in `paper` (or white over the red low-battery fill), where it sits
    over the empty part in `ink` -- so it stays readable at any level
    without a separate "97%" label next to the icon, which is what used
    to get squeezed out on pages with long titles."""
    bx1 = x1 - BATTERY_NUB_W
    bx0 = bx1 - BATTERY_BODY_W
    by0 = int(cy - BATTERY_BODY_H / 2)
    by1 = by0 + BATTERY_BODY_H
    d.rounded_rectangle([bx0, by0, bx1, by1], radius=3, outline=ink, width=1)
    d.rounded_rectangle([bx1 + 1, cy - 3, x1, cy + 3], radius=1, fill=ink)
    ix0, iy0, ix1, iy1 = bx0 + 2, by0 + 2, bx1 - 2, by1 - 2
    low = pct <= 15
    fill_x = ix0 + round((ix1 - ix0) * pct / 100)
    if fill_x > ix0:
        d.rectangle([ix0, iy0, fill_x, iy1], fill=low_color if low else ink)

    txt = str(pct)
    f = font("default_bold", 11 if pct < 100 else 10)
    w, h = bx1 - bx0 + 1, by1 - by0 + 1
    mask = Image.new("L", (w, h), 0)
    md = ImageDraw.Draw(mask)
    tb = md.textbbox((0, 0), txt, font=f)
    md.text(((w - (tb[2] - tb[0])) / 2 - tb[0], (h - (tb[3] - tb[1])) / 2 - tb[1]),
            txt, font=f, fill=255)
    split = fill_x - bx0 + 1                     # mask x where the fill ends
    over_fill, over_empty = mask.copy(), mask
    ImageDraw.Draw(over_fill).rectangle([split, 0, w, h], fill=0)
    ImageDraw.Draw(over_empty).rectangle([0, 0, split - 1, h], fill=0)
    d._image.paste((255, 255, 255) if low else paper, (bx0, by0), over_fill)
    d._image.paste(ink, (bx0, by0), over_empty)


def _draw_bolt(d, x0, cy, color):
    """Charging bolt, 7x13, vertically centred on cy."""
    y0 = cy - 6.5
    d.polygon([(x0 + 4.2, y0), (x0, y0 + 7.3), (x0 + 3.2, y0 + 7.3), (x0 + 2.4, y0 + 13),
               (x0 + 7, y0 + 5.4), (x0 + 3.8, y0 + 5.4)], fill=color)


HEADER_H = 34
HEADER_MID = HEADER_H // 2
HEADER_BAR_W, HEADER_BAR_GAP, HEADER_BAR_H = 4, 2, 14


def _header_text(d, x_right, text, f, color):
    """Right-aligned at x_right, vertically centred in the header."""
    tb = d.textbbox((0, 0), text, font=f)
    d.text((x_right - (tb[2] - tb[0]) - tb[0], HEADER_MID - (tb[3] + tb[1]) / 2), text, font=f, fill=color)


def draw_header(d, label, accent, conn_type=None, cell_signal=None):
    label, conn_type = tr(label), tr(conn_type)
    d.rectangle([0, 0, W, HEADER_H], fill=accent)
    f_title = font("default_bold", 18)
    d.text((14, 8), label, font=f_title, fill=BG)

    # Status items, right to left: battery (with its % inside) | WAN type
    # | cellular tech + bars -- all centred on the header's midline, in a
    # fixed-height 34px bar. Measured up front against the room the title
    # leaves; if they don't fit, the tech label beside the bars goes
    # first, then the bars. The battery and the WAN type always stay.
    f_conn = font("default_medium", 13)
    f_rat = font("default_medium", 12)
    bars_w = 4 * HEADER_BAR_W + 3 * HEADER_BAR_GAP
    battery = get_battery()
    rat = cell_signal[1] if cell_signal and cell_signal[1] != conn_type else None
    show = {"rat": bool(rat), "bars": bool(cell_signal)}

    def needed():
        parts = []                       # (width, gap to the next item left)
        if battery:
            parts.append((BATTERY_ICON_W + (10 if battery[1] else 0), 9))
        if conn_type:
            parts.append((d.textlength(conn_type, font=f_conn), 8))
        if show["rat"]:
            parts.append((d.textlength(rat, font=f_rat), 4))
        if show["bars"]:
            parts.append((bars_w, 0))
        return sum(w + g for w, g in parts) - (parts[-1][1] if parts else 0)

    room = (W - 12) - (14 + d.textlength(label, font=f_title) + 12)
    for item in ("rat", "bars"):
        if needed() <= room:
            break
        show[item] = False
    if needed() > room:
        f_conn = font("default_medium", 11)    # e.g. "OPENCLASH" + "Repeater"

    right_x = W - 12
    if battery:
        pct, plugged = battery
        _draw_battery_icon(d, right_x, HEADER_MID, pct, BG, accent, (190, 30, 30))
        right_x -= BATTERY_ICON_W + 3
        if plugged:
            _draw_bolt(d, right_x - 7, HEADER_MID, BG)
            right_x -= 10
        right_x -= 6

    if conn_type:
        _header_text(d, right_x, conn_type, f_conn, BG)
        right_x -= d.textlength(conn_type, font=f_conn) + 8

    if show["rat"]:
        _header_text(d, right_x, rat, f_rat, BG)
        right_x -= d.textlength(rat, font=f_rat) + 4
    if show["bars"]:
        dim = _mix(BG, accent, 0.55)
        draw_signal_bars(d, right_x - bars_w, HEADER_MID + HEADER_BAR_H / 2, cell_signal[0], BG, dim,
                         bar_w=HEADER_BAR_W, gap=HEADER_BAR_GAP, max_h=HEADER_BAR_H)


def draw_back_header(d, label, accent):
    label = tr(label)
    d.rectangle([0, 0, W, 34], fill=accent)
    d.text((12, 5), "‹", font=font("default_bold", 24, latin=True), fill=BG)
    d.text((32, 8), label, font=font("default_bold", 16), fill=BG)


def draw_toggle(d, x, y, on, accent, w=52, h=28):
    r = h / 2
    color = accent if on else (60, 65, 80)
    d.rounded_rectangle([x, y, x + w, y + h], radius=r, fill=color)
    knob_r = h / 2 - 3
    kx = x + w - r if on else x + r
    ky = y + h / 2
    d.ellipse([kx - knob_r, ky - knob_r, kx + knob_r, ky + knob_r], fill=(255, 255, 255))


def draw_segmented(d, x, y, w, h, labels, selected_idx, accent, fsize=14):
    d.rounded_rectangle([x, y, x + w, y + h], radius=h / 2, outline=accent, width=2)
    seg_w = w / len(labels)
    if selected_idx is not None:
        hx0 = x + selected_idx * seg_w
        d.rounded_rectangle([hx0 + 2, y + 2, hx0 + seg_w - 2, y + h - 2], radius=(h - 4) / 2, fill=accent)
    f = font("default_medium", fsize)
    for i, label in enumerate(labels):
        bbox = d.textbbox((0, 0), label, font=f)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        tx = x + i * seg_w + (seg_w - tw) / 2
        ty = y + (h - th) / 2 - bbox[1]
        color = BG if i == selected_idx else FG
        d.text((tx, ty), label, font=f, fill=color)


def centered_text(d, cx, y, text, f, fill):
    bbox = d.textbbox((0, 0), text, font=f)
    tw = bbox[2] - bbox[0]
    d.text((cx - tw / 2, y), text, font=f, fill=fill)


def centered_text_box(d, x0, y0, x1, y1, text, f, fill):
    """Centers on both axes within [x0,y0,x1,y1] -- unlike centered_text
    (horizontal-only, `y` is a hand-picked top offset), this accounts for
    the font's actual glyph bbox so it lands in the true middle of a
    button regardless of font metrics or box height."""
    bbox = d.textbbox((0, 0), text, font=f)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = (x0 + x1) / 2 - tw / 2
    ty = y0 + ((y1 - y0) - th) / 2 - bbox[1]
    d.text((tx, ty), text, font=f, fill=fill)


_truncate_cache = {}
_TRUNCATE_CACHE_MAX = 512


def truncate_to_width(d, text, f, max_w):
    """Longest prefix of `text` that fits in max_w with an ellipsis.

    Binary search over the prefix length, cached per (text, font, width).
    It used to drop one character at a time and re-measure the whole
    string on each step -- quadratic in the text, and the Home tile passes
    it long strings. Measuring with the font directly keeps `d` out of
    the cache key."""
    key = (text, f, max_w)
    hit = _truncate_cache.get(key)
    if hit is not None:
        return hit
    width = lambda s: f.getbbox(s)[2]
    if width(text) <= max_w:
        out = text
    else:
        lo, hi = 1, len(text) - 1            # lo always fits (or is the floor)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if width(text[:mid] + "…") <= max_w:
                lo = mid
            else:
                hi = mid - 1
        out = text[:lo] + "…"
    if len(_truncate_cache) >= _TRUNCATE_CACHE_MAX:
        _truncate_cache.clear()
    _truncate_cache[key] = out
    return out


def wrap_text_to_lines(d, text, f, max_w):
    """Character-by-character wrap so it works for CJK, which has no
    spaces between words -- but when the line being broken does contain a
    space, the break moves back to it, so English text wraps between
    words instead of mid-word ("rou / ter")."""
    lines = []
    for paragraph in text.split("\n"):
        line = ""
        for ch in paragraph:
            candidate = line + ch
            if line and d.textbbox((0, 0), candidate, font=f)[2] > max_w:
                cut = line.rfind(" ")
                if ch != " " and cut > 0:
                    lines.append(line[:cut])
                    line = line[cut + 1:] + ch
                else:
                    lines.append(line.rstrip())
                    line = "" if ch == " " else ch
            else:
                line = candidate
        lines.append(line)
    return lines


OC_TOGGLE_RECT = (172, 38, 218, 60)
OC_MODE_SEG_RECT = (16, 100, 224, 128)
OC_NODE_ZONE = (146, 192)



PICKER_TOP, PICKER_BOTTOM = 38, 316

PANEL_NAMES = list(MODULES)


# Charge-limit settings and status come from the independent controller.
def battery_snapshot():
    def read_json(path):
        try:
            return json.loads(Path(path).read_text())
        except (OSError, ValueError):
            return {}
    cfg = dict(enabled=False, threshold=80)
    cfg.update(read_json(CONFIG_DIR / "battery_config.json"))
    status = read_json("/var/run/mudi7-customizer/battery.json")
    return cfg, status


def battery_action(action, value):
    cmd = [sys.executable, str(Path(__file__).with_name("battery_control.py")), action, str(value)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
    return result.returncode == 0, (result.stderr.strip() or "Charge setting applied")


BATTERY_CURRENT_INTERVAL = 3.0


def read_battery_current():
    # The Mudi 7 CW221X driver reports signed milliamps.
    try:
        return int(_read_sys(f"{POWER_SUPPLY_DIR}/cw221X-bat/current_now"))
    except (OSError, ValueError, TypeError):
        return None


def panel_battery(conn_type=None, cell_signal=None, current=None):
    cfg, st = battery_snapshot()
    img, d = new_canvas()
    a = ACCENT["battery"]
    draw_header(d, tr("BATTERY"), a, conn_type, cell_signal)
    # The controller backs off to a 30 s heartbeat when no adapter is attached,
    # so allow one missed tick before calling the service dead.
    healthy = time.time() - st.get("updated", 0) < 45 and not st.get("error")
    pct = st.get("capacity")
    centered_text(d, 66, 42, f"{pct}%" if pct is not None else "--%", font("default_bold", 28), a)
    d.text((125, 47), tr("USB power") if st.get("online") else tr("On battery"), font=font("default_medium", 12), fill=FG)
    d.text((125, 65), tr("Mudi 7 scale"), font=font("default_medium", 11), fill=DIM)
    message = st.get("message", tr("Service unavailable")) if healthy else st.get("error") or tr("Service unavailable")
    centered_text(d, W / 2, 88, truncate_to_width(d, tr(message), font("default_medium", 12), 220), font("default_medium", 12), DIM)
    d.text((16, 116), tr("Charge limit"), font=font("default_bold", 15), fill=FG)
    draw_toggle(d, 174, 112, bool(cfg["enabled"]), a, w=50, h=26)
    centered_text(d, W / 2, 151, tr("Target"), font("default_medium", 12), DIM)
    for x0, x1, label in [(16, 66, "-"), (174, 224, "+")]:
        d.rounded_rectangle([x0, 173, x1, 207], radius=8, outline=a, width=2)
        centered_text_box(d, x0, 173, x1, 207, label, font("default_bold", 20), a)
    centered_text(d, W / 2, 176, str(cfg["threshold"]) + "%", font("default_bold", 22), FG)
    d.line((16, 225, 224, 225), fill=(40, 44, 54))
    centered_text(d, W / 2, 235, tr("Battery current"), font("default_medium", 12), DIM)
    current_text = "-- mA"
    if current is not None:
        current_text = f"{current:+d} mA" if current else "0 mA"
    centered_text(d, W / 2, 256, current_text, font("default_bold", 21), a)
    draw_page_dots(d, PANEL_NAMES.index("battery"))
    return img


def hit_main_battery(x, y):
    if 16 <= x <= 224:
        if 108 <= y <= 141:
            return "enable"
        if 173 <= y <= 207:
            return "minus" if x <= 66 else "plus" if x >= 174 else None
    return None


# ---------- main panels ----------

PIN_CHANGE_RECT = (16, 105, 224, 151)
PIN_DISABLE_RECT = (16, 170, 224, 216)
PIN_TITLES = {
    "unlock": "Enter PIN",
    "change": "Enter current PIN",
    "disable": "Enter current PIN",
    "new": "Set a four-digit PIN",
    "confirm": "Confirm new PIN",
}


def pin_keys():
    for i, label in enumerate(("1", "2", "3", "4", "5", "6", "7", "8", "9", "", "0", "Del")):
        if label:
            x, y = 16 + (i % 3) * 72, 110 + (i // 3) * 49
            yield label, (x, y, x + 64, y + 42)


def pin_hit(x, y, rect):
    x0, y0, x1, y1 = rect
    return x0 <= x <= x1 and y0 <= y <= y1


def pin_button(d, rect, label):
    accent = ACCENT["settings"]
    d.rounded_rectangle(rect, radius=8, fill=(22, 28, 40), outline=accent, width=1)
    centered_text_box(d, *rect, tr(label), font("default_bold", 16), FG)


def panel_pin_settings(enabled):
    img, d = new_canvas()
    draw_back_header(d, tr("Screen PIN"), ACCENT["settings"])
    centered_text(d, W / 2, 64, tr("Protection on") if enabled else tr("Protection off"),
                  font("default_medium", 14), DIM)
    pin_button(d, PIN_CHANGE_RECT, tr("Change PIN") if enabled else tr("Set PIN and enable"))
    if enabled:
        pin_button(d, PIN_DISABLE_RECT, tr("Turn off protection"))
    return img


def panel_pin_keypad(step, count=0, notice=""):
    img, d = new_canvas()
    if step == "unlock":
        draw_header(d, tr("LOCKED"), ACCENT["settings"])
    else:
        draw_back_header(d, tr("Screen PIN"), ACCENT["settings"])
    centered_text(d, W / 2, 43, tr(PIN_TITLES[step]), font("default_bold", 16), FG)
    for i in range(4):
        x = W / 2 - 27 + 18 * i
        d.ellipse((x - 4, 72, x + 4, 80), outline=DIM, fill=FG if i < count else None)
    centered_text(d, W / 2, 90, tr(notice), font("default_medium", 12), DIM)
    for label, rect in pin_keys():
        pin_button(d, rect, label)
    return img


SIM_REFRESH_INTERVAL = 3.0
# Three display tiers, based on Teltonika's LTE/5G signal recommendations.
SIM_SIGNAL_THRESHOLDS = {
    "rsrp": (-80, -100),
    "rsrq": (-10, -20),
    "sinr": (20, 0),
}
SIM_GOOD = (110, 220, 150)
SIM_FAIR = (255, 170, 90)
SIM_POOR = (255, 106, 106)


def sim_signal_color(key, value):
    if value is None:
        return DIM
    excellent, poor = SIM_SIGNAL_THRESHOLDS[key]
    if value >= excellent:
        return SIM_GOOD
    if value <= poor:
        return SIM_POOR
    return SIM_FAIR


# --- Composite signal grade ---------------------------------------------
# The modem does not report RSSI at all on LTE/NR (cell_info fills the
# field with the -32768 sentinel, which _cell_int turns into None), so the
# fourth cell of the signal grid used to sit at "--" forever. It now shows
# one grade derived from the three metrics that do report, which is also
# the more useful readout: no single number describes link quality.
#
# RSRP alone is just received power -- a strong, heavily interfered cell
# still looks great. SINR is the closest proxy for achievable throughput,
# and RSRQ folds in cell load and interference per resource block. So the
# score is a weighted blend (SINR heaviest), then capped by the weakest
# metric: a link is only as good as its bottleneck, and plain averaging
# would let a -70 dBm RSRP paper over a SINR of 0 dB.
#
# Each metric maps to 0-100 through a piecewise-linear curve anchored on
# the same planning guidance the per-metric colours use. Weights are
# renormalised over whichever metrics the modem actually returned, so a
# 3G registration that only reports RSRP still grades sensibly.
SIM_QUALITY_WEIGHTS = {"rsrp": 0.35, "rsrq": 0.25, "sinr": 0.40}
SIM_QUALITY_CURVES = {
    "rsrp": ((-130, 0), (-120, 10), (-110, 32), (-100, 58),
             (-90, 78), (-80, 92), (-70, 100)),
    "rsrq": ((-25, 0), (-20, 30), (-15, 62), (-10, 88), (-5, 100)),
    "sinr": ((-10, 0), (-5, 22), (0, 42), (5, 62), (13, 82),
             (20, 96), (25, 100)),
}
SIM_QUALITY_TIERS = ((75, "Good", SIM_GOOD), (45, "Fair", SIM_FAIR))
SIM_QUALITY_POOR = ("Poor", SIM_POOR)


def _quality_score(key, value):
    """0-100 score for one metric, linear between the curve's anchors."""
    points = SIM_QUALITY_CURVES[key]
    if value <= points[0][0]:
        return 0.0
    if value >= points[-1][0]:
        return 100.0
    for (lo_v, lo_s), (hi_v, hi_s) in zip(points, points[1:]):
        if lo_v <= value <= hi_v:
            span = hi_v - lo_v
            return lo_s + (hi_s - lo_s) * (value - lo_v) / span
    return 0.0


def carrier_quality(carrier):
    """(grade, score, color) for a serving carrier, or None when it
    reports none of RSRP/RSRQ/SINR. Grade is Good / Fair / Poor."""
    if not carrier:
        return None
    weighted, weight_sum, worst = 0.0, 0.0, 100.0
    for key, weight in SIM_QUALITY_WEIGHTS.items():
        value = carrier.get(key)
        if value is None:
            continue
        score = _quality_score(key, value)
        weighted += score * weight
        weight_sum += weight
        worst = min(worst, score)
    if not weight_sum:
        return None
    score = weighted / weight_sum
    # Bottleneck cap: one bad dimension drags the whole grade down, so a
    # great RSRP cannot average its way past a dead SINR.
    if worst < 25:
        score = min(score, 39)
    elif worst < 45:
        score = min(score, 74)
    for floor, grade, color in SIM_QUALITY_TIERS:
        if score >= floor:
            return grade, score, color
    return SIM_QUALITY_POOR[0], score, SIM_QUALITY_POOR[1]


SIM_BANDS_Y = 82
SIM_BAND_CHIP_H = 18
_NR_CHIP = (86, 182, 255)


def _draw_band_row(d, cell):
    carriers = cell.get("carriers") or []
    f = font("default_bold", 11)
    x = 16
    shown = 0
    for carrier in carriers:
        label = carrier["band"]
        width = d.textlength(label, font=f) + 12
        if x + width > W - 36:
            break
        color = _NR_CHIP if carrier["nr"] else ACCENT["sim"]
        d.rounded_rectangle((x, SIM_BANDS_Y, x + width, SIM_BANDS_Y + SIM_BAND_CHIP_H),
                            radius=8, outline=color, fill=color if shown == 0 else None)
        centered_text_box(d, x, SIM_BANDS_Y, x + width, SIM_BANDS_Y + SIM_BAND_CHIP_H,
                          label, f, BG if shown == 0 else color)
        x += width + 4
        shown += 1
    if len(carriers) > shown:
        d.text((x, SIM_BANDS_Y + 2), f"+{len(carriers) - shown}", font=f, fill=DIM)
    total = sum(c["mhz"] or 0 for c in carriers)
    caption = f"{len(carriers)} carrier" + ("s" if len(carriers) != 1 else "")
    if total:
        caption += f" / {total:g} MHz"
    if not carriers:
        caption = tr("No serving cell")
    d.text((16, 105), tr(caption), font=font("default_medium", 11), fill=DIM)


def panel_sim(sim, conn_type=None, cell_signal=None, cell=None):
    cell = cell or {}
    img, d = new_canvas()
    accent = ACCENT["sim"]
    draw_header(d, tr("CELLULAR"), accent, conn_type, cell_signal)
    f_carrier = font("default_cn_medium", 16)
    carrier = sim.get("carrier") or tr("No service")
    d.text((16, 41), truncate_to_width(d, carrier, f_carrier, 154), font=f_carrier, fill=FG)
    network = cell_network_label(cell)
    tech = " ".join(network) if network else "--"
    centered_text_box(d, 174, 39, 228, 62, tech, font("default_bold", 12), accent)
    slot = sim.get("slot") or "--"
    data = tr("Connected") if sim.get("data_up") else tr("Disconnected")
    d.text((16, 65), tr(f"Slot {slot} / Data {data}"), font=font("default_medium", 11), fill=DIM)
    _draw_band_row(d, cell)

    carriers = cell.get("carriers") or []
    primary = carriers[0] if carriers else {}
    metrics = (("RSRP", "rsrp", "dBm"), ("RSRQ", "rsrq", "dB"),
               ("SINR", "sinr", "dB"))
    for i, (label, key, unit) in enumerate(metrics):
        x, y = 16 + (i % 2) * 112, 130 + (i // 2) * 43
        d.text((x, y), label, font=font("default_medium", 10), fill=DIM)
        value = primary.get(key)
        text = f"{value} {unit}" if value is not None else "--"
        d.text((x, y + 14), text, font=font("default_mono_medium", 14),
               fill=sim_signal_color(key, value))
    # Fourth cell: one composite grade instead of the RSSI reading this
    # modem never reports (see carrier_quality).
    qx, qy = 128, 173
    d.text((qx, qy), tr("QUALITY"), font=font("default_medium", 10), fill=DIM)
    quality = carrier_quality(primary)
    if quality:
        grade, score, color = quality
        grade = tr(grade)
        f_grade = font("default_mono_medium", 14)
        d.text((qx, qy + 14), grade, font=f_grade, fill=color)
        f_score = font("default_mono_medium", 11)
        score_text = f"{score:.0f}"
        d.text((qx + d.textlength(grade, font=f_grade) + 5, qy + 16),
               score_text, font=f_score, fill=DIM)
    else:
        d.text((qx, qy + 14), "--", font=font("default_mono_medium", 14), fill=DIM)
    d.line((16, 124, W - 16, 124), fill=(40, 44, 54))
    d.line((16, 217, W - 16, 217), fill=(40, 44, 54))
    primary_bw = primary.get("mhz")
    values = (
        (tr("PCI / Channel"), f"{primary.get('pci') if primary.get('pci') is not None else '--'} / "
                          f"{primary.get('earfcn') if primary.get('earfcn') is not None else '--'}"),
        (tr("TAC / Cell ID"), f"{cell.get('tac') or '--'} / {cell.get('cell_id') or '--'}"),
        (tr("Primary BW"), f"{primary_bw:g} MHz" if primary_bw else "--"),
    )
    for i, (label, value) in enumerate(values):
        y = 223 + i * 24
        d.text((16, y), label, font=font("default_medium", 10), fill=DIM)
        f = font("default_mono_medium", 11)
        value = truncate_to_width(d, value or "--", f, 126)
        bbox = f.getbbox(value)
        d.text((W - 16 - bbox[2], y), value, font=f, fill=FG)
    draw_page_dots(d, PANEL_NAMES.index("sim"))
    return img


def panel_openclash(oc, traf, conn_type=None, cell_signal=None):
    img, d = new_canvas()
    draw_header(d, "OPENCLASH", ACCENT["openclash"], conn_type, cell_signal)

    if not oc["installed"]:
        centered_text(d, W / 2, 130, tr("OpenClash isn't installed"), font("default_medium", 14), DIM)
        centered_text(d, W / 2, 150, tr("on this device"), font("default_medium", 14), DIM)
        draw_page_dots(d, PANEL_NAMES.index("openclash"))
        return img

    d.text((16, 39), "OpenClash", font=font("default_medium", 16), fill=FG)
    status, status_color = openclash_status_text(oc)
    d.text((16, 58), tr(status), font=font("default_medium", 11), fill=status_color)
    tx0, ty0, tx1, ty1 = OC_TOGGLE_RECT
    draw_toggle(d, tx0, ty0, openclash_toggle_on(oc), ACCENT["openclash"], w=tx1 - tx0, h=ty1 - ty0)

    d.line([16, 72, W - 16, 72], fill=(40, 44, 54))

    d.text((16, 80), tr("Mode"), font=font("default_medium", 14), fill=DIM)
    sx0, sy0, sx1, sy1 = OC_MODE_SEG_RECT
    sel_idx = 0 if oc["mode"] == "global" else 1
    draw_segmented(d, sx0, sy0, sx1 - sx0, sy1 - sy0, [tr("Global"), tr("Rule")], sel_idx, ACCENT["openclash"])

    d.line([16, 138, W - 16, 138], fill=(40, 44, 54))

    group, title = openclash_selector("global" if sel_idx == 0 else "rule", traf)
    selection = traf["groups"].get(group)
    d.text((16, 146), tr(title) + ("  >" if screen_i18n.chinese() else "  ›"), font=font("default_medium", 14), fill=DIM)
    if not traf["running"]:
        value = tr("API unavailable") if oc["running"] else tr("Core stopped")
    elif selection is None:
        value = tr("Selector unavailable")
    else:
        value = selection["now"] or tr("No selection")
    f = font("default_cn_medium", 15)
    d.text((16, 164), truncate_to_width(d, value, f, W - 32), font=f,
           fill=FG if selection and selection["now"] else DIM)

    d.line([16, 192, W - 16, 192], fill=(40, 44, 54))

    d.text((16, 200), tr("Traffic (session)"), font=font("default_medium", 14), fill=DIM)
    if traf["running"] and traf["up_mb"] is not None:
        stats = f"↑ {traf['up_mb']:.1f} MB   ↓ {traf['down_mb']:.1f} MB"
        d.text((16, 220), stats, font=font("default_mono_medium", 15), fill=FG)
    else:
        d.text((16, 220), "—", font=font("default_mono_medium", 15), fill=DIM)

    d.line((16, 248, W - 16, 248), fill=(40, 44, 54))
    d.text((16, 260), tr("Mihomo core"), font=font("default_medium", 14), fill=DIM)
    running = oc["running"]
    centered_text(d, 180, 260, tr("Started") if running else tr("Stopped"),
                  font("default_bold", 14), ACCENT["openclash"] if running else DIM)
    draw_page_dots(d, PANEL_NAMES.index("openclash"))
    return img


# Official service pages: identifiers and names stay unchanged in both languages.
VENDOR_TITLES = {'adguardhome': 'AdGuard Home', 'tailscale': 'Tailscale'}
VENDOR_STATES = {
    'not_installed': 'Not installed', 'unavailable': 'Status unavailable',
    'disabled': 'Stopped', 'inactive': 'Enabled, but not running',
    'running': 'Running', 'running_disabled': 'Running (disabled in config)',
    'connected': 'Connected', 'connecting': 'Connecting…',
    'needs_login': 'Login required', 'needs_authorization': 'Authorization required',
}


def panel_vendor(name, service, conn_type=None, cell_signal=None):
    img, d = new_canvas()
    accent = ACCENT[name]
    draw_header(d, VENDOR_TITLES[name], accent, conn_type, cell_signal)
    d.text((16, 43), tr('Service'), font=font('default_medium', 14), fill=FG)
    if service.get('available'):
        draw_toggle(d, *OC_TOGGLE_RECT[:2], service['enabled'], accent, w=46, h=22)
    else:
        centered_text(d, 196, 43, '—', font('default_medium', 14), DIM)
    state = service.get('state', 'unavailable')
    color = accent if state in ('running', 'connected') else DIM
    status_text = tr(VENDOR_STATES.get(state, 'Status unavailable'))
    d.text((16, 78), truncate_to_width(d, status_text, font('default_medium', 12), 208),
           font=font('default_medium', 12), fill=color)
    d.line((16, 102, 224, 102), fill=(40, 44, 54))
    if name == 'adguardhome':
        for y, label, key in [(114, 'Requests', 'queries'), (180, 'Blocked', 'blocked')]:
            d.text((16, y), tr(label), font=font('default_medium', 13), fill=DIM)
            value = service.get(key)
            text = str(value) if value is not None else '—'
            f = font('default_bold', 26)
            number_color = (255, 106, 106) if key == 'blocked' and value is not None else accent
            d.text((16, y + 21), truncate_to_width(d, text, f, 208), font=f, fill=number_color)
        days = service.get('period_days')
        period = tr('Last %s day' if days == 1 else 'Last %s days').replace('%s', str(days)) if days else tr('Statistics period unavailable')
        centered_text(d, 120, 253, period, font('default_medium', 11), DIM)
        dns = service.get('dns_enabled')
        note = 'Client DNS handling enabled' if dns is True else 'Client DNS handling disabled' if dns is False else 'Statistics unavailable'
        if service.get('queries') is None:
            note = service.get('stats_error') or 'Statistics unavailable'
        centered_text(d, 120, 275, tr(note), font('default_medium', 10), DIM)
    else:
        d.text((16, 117), tr('Tailscale address'), font=font('default_medium', 13), fill=DIM)
        address = service.get('address') or '—'
        d.text((16, 142), truncate_to_width(d, address, font('default_mono_medium', 17), 208),
               font=font('default_mono_medium', 17), fill=FG)
        if state in ('needs_login', 'needs_authorization'):
            message = LOGIN_NOTICE if state == 'needs_login' else 'Authorize this device in the Tailscale WebUI.'
        elif not service.get('enabled'):
            message = 'Tailscale is off.'
        elif service.get('authenticated'):
            message = 'Manage network settings in the WebUI.'
        else:
            message = 'Complete login in the WebUI.'
        lines = wrap_text_to_lines(d, tr(message), font('default_medium', 12), 208)
        for i, line in enumerate(lines[:5]):
            centered_text(d, 120, 194 + i * 19, line, font('default_medium', 12), DIM)
    draw_page_dots(d, PANEL_NAMES.index(name))
    return img


def hit_vendor_toggle(x, y):
    return OC_TOGGLE_RECT[0] - 10 <= x <= OC_TOGGLE_RECT[2] + 10 and OC_TOGGLE_RECT[1] - 8 <= y <= OC_TOGGLE_RECT[3] + 8


def openclash_toggle_on(oc):
    return bool(oc.get("running"))


def openclash_status_text(oc):
    """One line under the OpenClash label saying what's really going on,
    since the toggle alone can't show "enabled but the core isn't up"."""
    running, busy, enabled = oc.get("running"), oc.get("busy"), oc.get("enabled")
    if busy:
        return ("Stopping…" if running and not enabled else "Starting…"), DIM
    if running:
        return ("Running" if enabled else "Running (disabled in config)"), ACCENT["openclash"]
    if enabled:
        return "Enabled, but not running", (230, 170, 90)
    return "Stopped", DIM


MONITOR_CPU_BAR = (16, 144, W - 16, 158)
MONITOR_RAM_BAR = (16, 200, W - 16, 214)


def panel_monitor(link_health, cpu_pct, ram_pct, ram_used_gb, ram_total_gb, temp_c, uptime_min, conn_type=None, cell_signal=None):
    img, d = new_canvas()
    draw_header(d, tr("MONITOR"), ACCENT["monitor"], conn_type, cell_signal)

    link = link_health or {}
    status = link.get("status", tr("Checking"))
    history = link.get("history", ())
    latest = history[-1] if history and status == "Reachable" else None
    latency = f"{latest:.0f} ms" if latest is not None else "—"
    loss = f"{history.count(None)} / {len(history)}" if history else "—"
    f_label = font("default_medium", 12)
    f_value = font("default_bold", 17)
    status_color = {
        "Reachable": (110, 220, 150),
        "No reply": (255, 170, 90),
        "No WAN": (255, 106, 106),
    }.get(status, DIM)
    d.text((16, 44), tr("Link Health"), font=f_label, fill=FG)
    status = tr(status)
    status_width = d.textlength(status, font=f_label)
    d.text((W - 16 - status_width, 44), status, font=f_label, fill=status_color)
    d.text((16, 64), tr("Latency"), font=f_label, fill=DIM)
    d.text((128, 64), tr("Packet Loss"), font=f_label, fill=DIM)
    d.text((16, 81), latency, font=f_value, fill=FG)
    d.text((128, 81), loss, font=f_value, fill=FG)

    d.line([16, 116, W - 16, 116], fill=(40, 44, 54))

    d.text((16, 124), tr("CPU"), font=font("default_medium", 14), fill=FG)
    cpu_txt = f"{cpu_pct:.0f}%" if cpu_pct is not None else "—"
    centered_text(d, W - 30, 124, cpu_txt, font("default_medium", 13), DIM)
    bx0, by0, bx1, by1 = MONITOR_CPU_BAR
    d.rounded_rectangle([bx0, by0, bx1, by1], radius=6, outline=DIM, width=1)
    if cpu_pct is not None:
        bw = int((bx1 - bx0 - 4) * cpu_pct / 100)
        if bw > 14:
            d.rounded_rectangle([bx0 + 2, by0 + 2, bx0 + 2 + bw, by1 - 2], radius=4, fill=ACCENT["monitor"])
        elif bw > 0:
            # PIL's rounded_rectangle breaks ("x1 must be >= x0") when the
            # shape is thinner than ~2x the corner radius -- plain
            # rectangle for anything that thin (same fix as elsewhere in
            # this file for the same underlying PIL quirk).
            d.rectangle([bx0 + 2, by0 + 2, bx0 + 2 + bw, by1 - 2], fill=ACCENT["monitor"])

    d.text((16, 180), tr("RAM"), font=font("default_medium", 14), fill=FG)
    ram_txt = f"{ram_used_gb:.1f} / {ram_total_gb:.1f} GB" if ram_pct is not None else "—"
    centered_text(d, W - 60, 180, ram_txt, font("default_medium", 12), DIM)
    bx0, by0, bx1, by1 = MONITOR_RAM_BAR
    d.rounded_rectangle([bx0, by0, bx1, by1], radius=6, outline=DIM, width=1)
    if ram_pct is not None:
        bw = int((bx1 - bx0 - 4) * ram_pct / 100)
        if bw > 14:
            d.rounded_rectangle([bx0 + 2, by0 + 2, bx0 + 2 + bw, by1 - 2], radius=4, fill=ACCENT["monitor"])
        elif bw > 0:
            d.rectangle([bx0 + 2, by0 + 2, bx0 + 2 + bw, by1 - 2], fill=ACCENT["monitor"])

    d.line([16, 230, W - 16, 230], fill=(40, 44, 54))

    f_row = font("default_medium", 15)
    temp_txt = f"{temp_c:.0f}°C" if temp_c is not None else "—"
    d.text((16, 240), tr("Temp"), font=f_row, fill=FG)
    d.text((76, 240), temp_txt, font=f_row, fill=FG)
    up_h, up_m = divmod(uptime_min, 60)
    up_txt = f"{up_h // 24}d {up_h % 24}h" if up_h >= 48 else f"{up_h}h {up_m}m"
    d.text((16, 264), tr("Uptime"), font=font("default_medium", 12) if screen_i18n.chinese() else f_row, fill=FG)
    uptime_font = font("default_medium", 12) if screen_i18n.chinese() else f_row
    uptime_text = truncate_to_width(d, tr(up_txt), uptime_font, 60)
    d.text((76, 264), uptime_text, font=uptime_font, fill=FG)

    x0, y0, x1, y1 = MONITOR_SPEEDTEST_BTN
    d.rounded_rectangle([x0, y0, x1, y1], radius=10, fill=(22, 28, 40), outline=(42, 48, 60), width=1)
    _icon_speedometer(d, (x0 + x1) / 2, y0 + 17, 11, ACCENT["monitor"])
    centered_text(d, (x0 + x1) / 2, y0 + 27, tr("Speed test"), font("default_bold", 12), FG)

    draw_page_dots(d, PANEL_NAMES.index("monitor"))
    return img


MONITOR_SPEEDTEST_BTN = (140, 238, W - 16, 284)


def hit_main_monitor(x, y):
    x0, y0, x1, y1 = MONITOR_SPEEDTEST_BTN
    if x0 - 4 <= x <= x1 + 4 and y0 - 4 <= y <= y1 + 4:
        return "speedtest"
    return None


def _icon_speedometer(d, cx, cy, r, color):
    """Half-dial with a needle, anti-aliased."""
    stroke = max(2.0, r * 0.2)
    pad = stroke + 1
    x0, y0 = cx - r - pad, cy - r - pad
    w, h = int(2 * (r + pad)) + 2, int(r + 2 * pad) + 2

    def paint(m, s):
        ox, oy = (cx - int(round(x0))) * s, (cy - int(round(y0))) * s
        R, half = r * s, stroke * s / 2
        m.arc([ox - R - half, oy - R - half, ox + R + half, oy + R + half],
              start=180, end=360, fill=255, width=int(round(stroke * s)))
        for a in (180, 360):
            ex, ey = ox + R * math.cos(math.radians(a)), oy + R * math.sin(math.radians(a))
            m.ellipse([ex - half, ey - half, ex + half, ey + half], fill=255)
        na = math.radians(305)
        m.line([ox, oy, ox + R * 0.8 * math.cos(na), oy + R * 0.8 * math.sin(na)],
               fill=255, width=int(round(stroke * s)))
        hub = stroke * 0.9 * s
        m.ellipse([ox - hub, oy - hub, ox + hub, oy + hub], fill=255)

    _draw_aa(d, x0, y0, w, h, color, paint)


# ---------- speed test screen ----------

SPEEDTEST_GAUGE_C = (W // 2, 128)
SPEEDTEST_GAUGE_R = 70
SPEEDTEST_GAUGE_STROKE = 10
SPEEDTEST_GAUGE_START = 150          # PIL degrees (clockwise from 3 o'clock)
SPEEDTEST_GAUGE_SWEEP = 240
# Gauge scale: evenly spaced stops rather than linear, so 5 Mbps and
# 500 Mbps are both readable on the same dial.
SPEEDTEST_SCALE = [0, 5, 10, 25, 50, 100, 250, 500, 1000]
SPEEDTEST_SCALE_LABELS = [0, 10, 50, 250, 1000]
SPEEDTEST_DOWN_COLOR = ACCENT["settings"]
SPEEDTEST_UP_COLOR = ACCENT["sim"]
SPEEDTEST_DOWN_CARD = (16, 196, 116, 244)
SPEEDTEST_UP_CARD = (124, 196, W - 16, 244)
SPEEDTEST_BUTTON = (40, 256, W - 40, 288)


def _speed_frac(mbps):
    """0..1 along the gauge for a speed, piecewise-linear between stops."""
    if mbps is None or mbps <= 0:
        return 0.0
    stops = SPEEDTEST_SCALE
    if mbps >= stops[-1]:
        return 1.0
    for i in range(1, len(stops)):
        if mbps <= stops[i]:
            seg = (mbps - stops[i - 1]) / (stops[i] - stops[i - 1])
            return (i - 1 + seg) / (len(stops) - 1)
    return 1.0


def format_mbps(mbps):
    if mbps is None:
        return "—"
    return f"{mbps:.1f}" if mbps < 100 else f"{mbps:.0f}"


def _draw_speed_gauge(d, frac, color):
    cx, cy = SPEEDTEST_GAUGE_C
    r, stroke = SPEEDTEST_GAUGE_R, SPEEDTEST_GAUGE_STROKE
    a0, sweep = SPEEDTEST_GAUGE_START, SPEEDTEST_GAUGE_SWEEP
    pad = stroke
    x0, y0 = cx - r - pad, cy - r - pad
    size = int(2 * (r + pad)) + 2

    def arc_mask(start, end):
        def paint(m, s):
            ox, oy = (cx - int(round(x0))) * s, (cy - int(round(y0))) * s
            R, half = r * s, stroke * s / 2
            m.arc([ox - R - half, oy - R - half, ox + R + half, oy + R + half],
                  start=start, end=end, fill=255, width=int(round(stroke * s)))
            for a in (start, end):      # round caps
                ex, ey = ox + R * math.cos(math.radians(a)), oy + R * math.sin(math.radians(a))
                m.ellipse([ex - half, ey - half, ex + half, ey + half], fill=255)
        return paint

    _draw_aa(d, x0, y0, size, size, (34, 40, 54), arc_mask(a0, a0 + sweep))
    if frac > 0.004:
        _draw_aa(d, x0, y0, size, size, color, arc_mask(a0, a0 + sweep * frac))

    f_tick = font("default_medium", 9)
    n = len(SPEEDTEST_SCALE) - 1
    for val in SPEEDTEST_SCALE_LABELS:
        i = SPEEDTEST_SCALE.index(val)
        a = math.radians(a0 + sweep * i / n)
        tr = r - stroke - 9
        tx, ty = cx + tr * math.cos(a), cy + tr * math.sin(a)
        txt = "1G" if val == 1000 else str(val)
        tw = d.textlength(txt, font=f_tick)
        d.text((tx - tw / 2, ty - 6), txt, font=f_tick, fill=DIM)


def _draw_pill(d, x0, y0, x1, y1, fill):
    """Fully-rounded bar of any length. Not rounded_rectangle: Pillow 9.5
    (what this firmware ships) raises "x1 must be greater than or equal
    to x0" when a rounded rectangle is about as short as its corner
    diameter -- which a progress bar always is right as it starts. That
    exception took the whole dashboard down the moment a speed test
    began."""
    h = y1 - y0
    if x1 - x0 <= h:
        d.ellipse([x0, y0, x0 + max(x1 - x0, 1), y1], fill=fill)
        return
    d.ellipse([x0, y0, x0 + h, y1], fill=fill)
    d.ellipse([x1 - h, y0, x1, y1], fill=fill)
    d.rectangle([x0 + h / 2, y0, x1 - h / 2, y1], fill=fill)


def _draw_arrow(d, cx, cy, up, color, size=6):
    """Solid up/down arrow -- drawn, not a glyph, so it lines up exactly."""
    s = size
    if up:
        d.polygon([(cx, cy - s), (cx - s, cy), (cx - s / 2.6, cy), (cx - s / 2.6, cy + s),
                   (cx + s / 2.6, cy + s), (cx + s / 2.6, cy), (cx + s, cy)], fill=color)
    else:
        d.polygon([(cx, cy + s), (cx - s, cy), (cx - s / 2.6, cy), (cx - s / 2.6, cy - s),
                   (cx + s / 2.6, cy - s), (cx + s / 2.6, cy), (cx + s, cy)], fill=color)


def panel_speedtest(snap, spin_phase=0.0):
    img, d = new_canvas()
    draw_back_header(d, tr("Speed test"), ACCENT["monitor"])
    phase = snap["phase"]
    running = phase in ("download", "upload")
    cx, cy = SPEEDTEST_GAUGE_C

    if running:
        color = SPEEDTEST_DOWN_COLOR if phase == "download" else SPEEDTEST_UP_COLOR
        value = snap["live"]
        caption = tr("Download") if phase == "download" else tr("Upload")
    elif phase == "done":
        color, value, caption = SPEEDTEST_DOWN_COLOR, snap["down"], tr("Download")
    else:
        color, value, caption = SPEEDTEST_DOWN_COLOR, None, tr("Ready")
    _draw_speed_gauge(d, _speed_frac(value), color)

    f_cap = font("default_medium", 12)
    if running:
        tw = d.textlength(caption, font=f_cap)
        _draw_arrow(d, cx - tw / 2 - 8, cy - 22, phase == "upload", color, size=5)
        d.text((cx - tw / 2 + 2, cy - 29), caption, font=f_cap, fill=color)
    else:
        centered_text(d, cx, cy - 29, caption, f_cap, DIM)
    centered_text(d, cx, cy - 12, format_mbps(value) if value is not None else "—",
                  font("default_bold", 30), FG)
    centered_text(d, cx, cy + 22, "Mbps", f_cap, DIM)
    if running:
        # thin progress bar for the current phase, under the dial opening
        bw = 80
        bx0 = cx - bw / 2
        _draw_pill(d, bx0, cy + 44, bx0 + bw, cy + 48, (34, 40, 54))
        pw = bw * snap["progress"]
        if pw > 0:
            _draw_pill(d, bx0, cy + 44, bx0 + pw, cy + 48, color)

    for rect, label, key, up, col in ((SPEEDTEST_DOWN_CARD, tr("Download"), "down", False, SPEEDTEST_DOWN_COLOR),
                                      (SPEEDTEST_UP_CARD, tr("Upload"), "up", True, SPEEDTEST_UP_COLOR)):
        x0, y0, x1, y1 = rect
        active = running and phase == ("upload" if up else "download")
        d.rounded_rectangle([x0, y0, x1, y1], radius=10, fill=(22, 28, 40),
                            outline=col if active else (42, 48, 60), width=1)
        _draw_arrow(d, x0 + 14, y0 + 13, up, col, size=5)
        d.text((x0 + 24, y0 + 6), label, font=font("default_medium", 11), fill=DIM)
        val = snap[key]
        if val is not None:
            txt = format_mbps(val)
            f_val = font("default_bold", 18)
            d.text((x0 + 10, y0 + 22), txt, font=f_val, fill=FG)
            d.text((x0 + 14 + d.textlength(txt, font=f_val), y0 + 28), "Mbps",
                   font=font("default_medium", 10), fill=DIM)
        elif active:
            draw_ring_spinner(d, x0 + 20, y0 + 33, 7, spin_phase, col, width=2)
        else:
            d.text((x0 + 10, y0 + 22), "—", font=font("default_bold", 18), fill=DIM)

    bx0, by0, bx1, by1 = SPEEDTEST_BUTTON
    if running:
        d.rounded_rectangle([bx0, by0, bx1, by1], radius=8, outline=DIM, width=2)
        centered_text_box(d, bx0, by0, bx1, by1, tr("Cancel"), font("default_medium", 14), DIM)
    else:
        d.rounded_rectangle([bx0, by0, bx1, by1], radius=8, fill=ACCENT["monitor"])
        centered_text_box(d, bx0, by0, bx1, by1, tr("Start") if phase == "idle" else tr("Run again"),
                          font("default_bold", 14), BG)

    f_foot = font("default_medium", 10)
    if phase == "error":
        foot, fcol = snap["error"] or tr("Test failed"), (255, 150, 150)
    elif phase == "cancelled":
        foot, fcol = tr("Cancelled"), DIM
    elif phase == "idle":
        foot, fcol = tr("~12s · Cloudflare · uses up to ~330 MB"), DIM
    else:
        via = f"via {snap['via']} · " if snap.get("via") else ""
        foot, fcol = f"{via}{snap['bytes'] / 1e6:.0f} MB used", DIM
    centered_text(d, W / 2, 298, truncate_to_width(d, tr(foot), f_foot, W - 24), f_foot, fcol)
    return img


def hit_speedtest_button(x, y):
    x0, y0, x1, y1 = SPEEDTEST_BUTTON
    return x0 - 6 <= x <= x1 + 6 and y0 - 6 <= y <= y1 + 6


# ---------- confirmation dialogs ----------

CONFIRM_YES_RECT = (30, 190, 210, 226)
CONFIRM_NO_RECT = (30, 236, 210, 272)


def panel_confirm(title, message, accent, yes_label="Yes", danger=False, acknowledge_only=False):
    img, d = new_canvas()
    if acknowledge_only:
        draw_header(d, title, accent)
    else:
        draw_back_header(d, title, accent)
    # centered_text draws a single line with no wrapping -- a message
    # longer than "Reboot the router now?" (the button was written and
    # tested against) ran off both edges of the 240px screen instead of
    # wrapping, e.g. "Hand the screen back to the GL.iNet UI?". Wrap it
    # and stack the lines centered in the space above the Yes/Cancel
    # buttons instead of assuming everything fits on one line.
    f_msg = font("default_medium", 15)
    lines = wrap_text_to_lines(d, tr(message), f_msg, W - 32)
    line_h = 20
    y0 = 110 - (len(lines) - 1) * line_h / 2
    for i, line in enumerate(lines):
        centered_text(d, W / 2, y0 + i * line_h, line, f_msg, FG)

    yx0, yy0, yx1, yy1 = CONFIRM_YES_RECT
    yes_color = (200, 80, 80) if danger else accent
    d.rounded_rectangle([yx0, yy0, yx1, yy1], radius=8, fill=yes_color)
    centered_text(d, (yx0 + yx1) / 2, yy0 + 9, tr(yes_label), font("default_bold", 14), BG)

    if acknowledge_only:
        return img

    nx0, ny0, nx1, ny1 = CONFIRM_NO_RECT
    d.rounded_rectangle([nx0, ny0, nx1, ny1], radius=8, outline=DIM, width=2)
    centered_text(d, (nx0 + nx1) / 2, ny0 + 9, tr("Cancel"), font("default_medium", 14), DIM)
    return img


def hit_confirm(x, y, acknowledge_only=False):
    yx0, yy0, yx1, yy1 = CONFIRM_YES_RECT
    if yx0 <= x <= yx1 and yy0 <= y <= yy1:
        return "yes"
    if acknowledge_only:
        return None
    nx0, ny0, nx1, ny1 = CONFIRM_NO_RECT
    if nx0 <= x <= nx1 and ny0 <= y <= ny1:
        return "no"
    return None


# ---------- Settings ----------

SETTINGS_TOP = 40
SETTINGS_ROW_H = 43
SETTINGS_ACTIONS = ("acceleration", "screen_settings", "pin_settings", "return_stock", "reboot", "shutdown")

SETTINGS_CONFIRM_ACTIONS = {
    "return_stock": ("Stock UI", "Hand the screen back to the GL.iNet UI?", "Switch"),
    "reboot": ("Reboot", "Reboot the router now?", "Reboot"),
    "shutdown": ("Shutdown", "Shut down the router now?", "Shutdown"),
}


def panel_settings(acceleration, pin_enabled, conn_type=None, cell_signal=None):
    img, d = new_canvas()
    accent = ACCENT["settings"]
    draw_header(d, tr("SETTINGS"), accent, conn_type, cell_signal)
    rows = (
        (tr("Network Accel."), None),
        (tr("Screen"), ""),
        (tr("Screen PIN"), tr("On") if pin_enabled else tr("Off")),
        (tr("Stock UI"), tr("Return")),
        (tr("Reboot"), ""),
        (tr("Shutdown"), ""),
    )
    for i, (label, value) in enumerate(rows):
        y = SETTINGS_TOP + i * SETTINGS_ROW_H
        color = (220, 100, 100) if i >= 4 else FG
        f = font("default_medium", 14)
        bbox = f.getbbox(label)
        text_y = y + (SETTINGS_ROW_H - (bbox[3] - bbox[1])) / 2 - bbox[1]
        if i == 0:
            text_y = y + 3
        d.text((16, text_y), label, font=f, fill=color)
        if i == 0:
            mode = tr(ACCELERATION_MODES.get(acceleration["actype"], tr("Unavailable")))
            mode_label = mode + "  >" if acceleration["enable"] else mode
            d.text((16, y + 24), mode_label, font=font("default_medium", 11), fill=DIM)
            if acceleration["available"]:
                draw_toggle(d, 176, y + 9, acceleration["enable"], accent, w=48, h=25)
            else:
                centered_text_box(d, 155, y, 226, y + SETTINGS_ROW_H, "--", font("default_medium", 13), DIM)
        else:
            text = (value + "  >") if value else ">"
            centered_text_box(d, 153, y, 226, y + SETTINGS_ROW_H, text, font("default_medium", 13), DIM)
        d.line((12, y + SETTINGS_ROW_H, W - 12, y + SETTINGS_ROW_H), fill=(40, 44, 54))
    draw_page_dots(d, PANEL_NAMES.index("settings"))
    return img


def hit_settings(x, y, acceleration):
    if not 8 <= x <= W - 8:
        return None
    index = int((y - SETTINGS_TOP) // SETTINGS_ROW_H)
    if not 0 <= index < len(SETTINGS_ACTIONS):
        return None
    if index == 0 and x < 164:
        return "acceleration_mode" if acceleration["available"] and acceleration["enable"] else None
    return SETTINGS_ACTIONS[index]


SCREEN_TIMEOUTS = {0: "Never", 15: "15 seconds", 30: "30 seconds", 60: "1 minute",
                   120: "2 minutes", 300: "5 minutes", 600: "10 minutes"}


def screen_brightness():
    maximum = int(_read_sys(str(Path(BACKLIGHT_PATH).with_name("max_brightness"))) or "0")
    raw = int(_read_sys(BACKLIGHT_PATH) or "0")
    if raw == 0:
        raw = int(_read_sys(str(CONFIG_DIR / "backlight_saved")) or str(maximum))
    return max(10, min(100, round(raw * 100 / maximum))) if maximum > 0 else 100


def set_screen_brightness(percent):
    if type(percent) is not int or not 10 <= percent <= 100:
        raise ValueError("Brightness must be 10–100%")
    maximum = int(_read_sys(str(Path(BACKLIGHT_PATH).with_name("max_brightness"))) or "0")
    if maximum <= 0:
        raise OSError("Brightness control unavailable")
    raw = max(1, round(maximum * percent / 100))
    saved = CONFIG_DIR / "backlight_saved"
    saved.parent.mkdir(parents=True, exist_ok=True)
    temp = saved.with_suffix(".new")
    temp.write_text(f"{raw}\n")
    temp.replace(saved)
    if int(_read_sys(BACKLIGHT_PATH) or "0") > 0:
        Path(BACKLIGHT_PATH).write_text(f"{raw}\n")


def panel_screen_settings(cfg):
    img, d = new_canvas()
    draw_back_header(d, tr("Screen"), ACCENT["settings"])
    brightness = cfg["screen_brightness"]
    if brightness is None:
        brightness = screen_brightness()
    rows = ((tr("Brightness"), f"{brightness}%"), (tr("Screen timeout"), tr(SCREEN_TIMEOUTS[cfg["screen_timeout"]])))
    for i, (label, value) in enumerate(rows):
        y = SETTINGS_TOP + i * 56
        d.text((16, y + 5), label, font=font("default_medium", 15), fill=FG)
        d.text((16, y + 28), value + "  >", font=font("default_medium", 12), fill=DIM)
        d.line((12, y + 56, W - 12, y + 56), fill=(40, 44, 54))
    return img


def panel_acceleration_modes(acceleration, scroll_px=0):
    items = [(mode, tr(ACCELERATION_MODES[mode])) for mode in acceleration["list"]]
    return panel_scroll_picker(tr("Acceleration"), ACCENT["settings"], items,
                               acceleration["actype"], scroll_px)


SCROLL_ROW_H = 32
SCROLL_FONT_SIZE = 15


def panel_scroll_picker(title, accent, items, selected, scroll_px, font_name="default_medium"):
    """Like panel_picker but with a fixed, larger row height and a vertical
    scroll offset -- for lists too long to shrink-to-fit on one screen.
    `font_name` lets callers with non-Latin content (e.g. Chinese-named
    OpenClash nodes) swap in a CJK-capable font without affecting the
    other pickers that share this renderer."""
    img, d = new_canvas()
    draw_back_header(d, title, accent)

    list_h = PICKER_BOTTOM - PICKER_TOP
    list_img = Image.new("RGB", (W, list_h), BG)
    ld = ImageDraw.Draw(list_img)
    f = font(font_name, SCROLL_FONT_SIZE)
    check_f = font("default_medium", SCROLL_FONT_SIZE, latin=True)  # CJK fonts may lack a ✓ glyph
    max_label_w = W - 56  # room for left margin + checkmark on the right
    for i, (key, label) in enumerate(items):
        y0 = i * SCROLL_ROW_H - scroll_px
        if y0 + SCROLL_ROW_H < 0 or y0 > list_h:
            continue
        sel = key == selected
        if sel:
            ld.rectangle([0, y0, W, y0 + SCROLL_ROW_H], fill=(28, 40, 56))
        if font_name != "default_cn_medium":
            label = tr(label)
        label = truncate_to_width(ld, label, f, max_label_w)
        bbox = ld.textbbox((0, 0), label, font=f)
        th = bbox[3] - bbox[1]
        color = accent if sel else FG
        ld.text((20, y0 + (SCROLL_ROW_H - th) / 2 - bbox[1]), label, font=f, fill=color)
        if sel:
            cbbox = ld.textbbox((0, 0), "✓", font=check_f)
            cth = cbbox[3] - cbbox[1]
            ld.text((W - 30, y0 + (SCROLL_ROW_H - cth) / 2 - cbbox[1]), "✓", font=check_f, fill=accent)
        if i > 0:
            ld.line([0, y0, W, y0], fill=(28, 32, 42))
    img.paste(list_img, (0, PICKER_TOP))

    content_h = len(items) * SCROLL_ROW_H
    max_scroll = max(0, content_h - list_h)
    if max_scroll > 0:
        thumb_h = max(20, list_h * list_h / content_h)
        thumb_y = PICKER_TOP + (scroll_px / max_scroll) * (list_h - thumb_h)
        # plain rectangle, not rounded -- too thin (4px) for PIL's corner math,
        # which raises ValueError on some Pillow builds at this aspect ratio
        d.rectangle([W - 6, thumb_y, W - 2, thumb_y + thumb_h], fill=(70, 76, 90))
    return img


def scroll_picker_max(n_items):
    return max(0, n_items * SCROLL_ROW_H - (PICKER_BOTTOM - PICKER_TOP))


def hit_scroll_picker(y, n_items, scroll_px):
    if not (PICKER_TOP <= y < PICKER_BOTTOM):
        return None
    idx = int((y - PICKER_TOP + scroll_px) / SCROLL_ROW_H)
    return idx if 0 <= idx < n_items else None


def panel_node_picker(traf, mode, scroll_px=0):
    group, title = openclash_selector(mode, traf)
    selection = traf["groups"].get(group)
    if selection and selection["all"]:
        items = [(name, name) for name in selection["all"]]
        return panel_scroll_picker(title, ACCENT["openclash"], items, selection["now"], scroll_px,
                                   font_name="default_cn_medium")
    img, d = new_canvas()
    draw_back_header(d, title, ACCENT["openclash"])
    message = tr("Mihomo API unavailable") if not traf["running"] else (
        tr("Selector unavailable") if selection is None else tr("No choices available"))
    centered_text(d, W / 2, 140, message, font("default_medium", 14), DIM)
    return img


def render_sub(view, cfg, oc, traf):
    if view == "oc_nodes":
        return panel_node_picker(traf, oc["mode"])


# ---------- hit-testing ----------

def hit_main_openclash(x, y):
    tx0, ty0, tx1, ty1 = OC_TOGGLE_RECT
    if tx0 - 10 <= x <= tx1 + 10 and ty0 - 8 <= y <= ty1 + 8:
        return "toggle"
    sx0, sy0, sx1, sy1 = OC_MODE_SEG_RECT
    if sx0 <= x <= sx1 and sy0 <= y <= sy1:
        return "mode_global" if x < (sx0 + sx1) / 2 else "mode_rule"
    if OC_NODE_ZONE[0] <= y < OC_NODE_ZONE[1]:
        return "node"
    return None


def hit_back(y):
    return y < 34


def to_rgb565_bytes(img):
    arr = np.asarray(img.convert("RGB"), dtype=np.uint32)
    r = (arr[:, :, 0] >> 3) << 11
    g = (arr[:, :, 1] >> 2) << 5
    b = (arr[:, :, 2] >> 3)
    packed = (r | g | b).astype("<u2")
    return packed.tobytes()


_fb = None


def _fb_handle():
    """Keep /dev/fb0 open for the process lifetime instead of reopening it
    per frame. During a drag this runs ~80x/s; open+close per frame is
    pure syscall overhead in the one path where smoothness matters most.
    Falls back to per-frame opens if the handle ever goes bad."""
    global _fb
    if _fb is None:
        _fb = open(FB_PATH, "r+b", buffering=0)
    return _fb


def write_frame(img):
    data = to_rgb565_bytes(img)
    global _fb
    try:
        fb = _fb_handle()
        fb.seek(0)
        fb.write(data)
    except Exception:
        try:
            if _fb is not None:
                _fb.close()
        except Exception:
            pass
        _fb = None
        with open(FB_PATH, "r+b") as fb:
            fb.write(data)


# ---------- modes ----------

def mode_preview(outdir):
    os.makedirs(outdir, exist_ok=True)
    cfg = load_config()
    sim = get_sim_status()
    oc = get_openclash_status()
    traf = get_openclash_traffic_and_node()
    sysinfo = get_system_info()
    acceleration = get_network_acceleration()
    cpu_sample, _ = sample_cpu(None)
    time.sleep(1)
    _, cpu_pct = sample_cpu(cpu_sample)
    ram_pct, ram_used_gb, ram_total_gb = get_ram_stats()
    temp_c = get_temp_c()
    _cell_info = _get_active_cell_info()
    conn_type = get_wan_conn_type(_cell_info)
    cell_signal = get_cell_signal(_cell_info)
    vendor_preview = VendorServices(gl_lua_rpc)
    screens = [
        ("adguardhome", panel_vendor("adguardhome", vendor_preview.snapshot("adguardhome"), conn_type, cell_signal)),
        ("tailscale", panel_vendor("tailscale", vendor_preview.snapshot("tailscale"), conn_type, cell_signal)),
        ("sim", panel_sim(sim, conn_type, cell_signal, _cell_info)),
        ("monitor", panel_monitor(None, cpu_pct, ram_pct, ram_used_gb, ram_total_gb, temp_c, sysinfo["uptime_min"], conn_type, cell_signal)),
        ("battery", panel_battery()),
        ("openclash", panel_openclash(oc, traf, conn_type, cell_signal)),
        ("settings", panel_settings(acceleration, False, conn_type, cell_signal)),
        ("speedtest", panel_speedtest(SpeedTest().snapshot())),
        ("speedtest_running", panel_speedtest({"phase": "upload", "live": 38.4, "progress": 0.45, "down": 212.6,
                                               "up": None, "bytes": 171e6, "error": None, "via": conn_type}, 90)),
        ("speedtest_done", panel_speedtest({"phase": "done", "live": 0.0, "progress": 1.0, "down": 212.6,
                                            "up": 41.3, "bytes": 205e6, "error": None, "via": conn_type})),
        ("oc_default_proxy", panel_node_picker(traf, "rule")),
        ("oc_global_node", panel_node_picker(traf, "global")),
        ("confirm", panel_confirm("Reboot", "Reboot the router now?", ACCENT["settings"], yes_label="Reboot", danger=True)),
        ("confirm_long", panel_confirm("Stock UI", "Hand the screen back to the GL.iNet UI?", ACCENT["settings"], yes_label="Switch")),
        ("screen_settings", panel_screen_settings(cfg)),
        ("pin_settings", panel_pin_settings(True)),
        ("pin_locked", panel_pin_keypad("unlock")),
    ]
    cols, pad = 5, 10
    rows = (len(screens) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (W + pad) + pad, rows * (H + pad) + pad), (30, 30, 30))
    for i, (name, img) in enumerate(screens):
        img.save(os.path.join(outdir, f"panel_{name}.png"))
        r, c = divmod(i, cols)
        sheet.paste(img, (pad + c * (W + pad), pad + r * (H + pad)))
    sheet.save(os.path.join(outdir, "contact_sheet.png"))
    print(f"wrote {len(screens)} preview PNGs to {outdir}")


def mode_calibrate():
    for name, color in [("RED", (255, 0, 0)), ("GREEN", (0, 255, 0)), ("BLUE", (0, 0, 255))]:
        img = Image.new("RGB", (W, H), color)
        write_frame(img)
        print(f"showing {name}")
        time.sleep(2)


_stop = False


def _on_term(signum, frame):
    global _stop
    _stop = True


# ---------- touch input ----------
# chsc_cap_touch on /dev/input/event0, Multitouch protocol B, confirmed by
# live capture on 2026-07-25: ABS_MT_POSITION_X=53, ABS_MT_POSITION_Y=54,
# ABS_MT_TRACKING_ID=57 (-1 on lift), coordinates in raw screen pixels.
TOUCH_DEV = "/dev/input/event0"
_EVENT_FMT = "qqHHi"
_EVENT_SIZE = struct.calcsize(_EVENT_FMT)
EV_ABS = 3
ABS_MT_SLOT = 47
ABS_MT_POSITION_X = 53
ABS_MT_POSITION_Y = 54
ABS_MT_TRACKING_ID = 57

TAP_JITTER_PX = 10

# Flick paging: a short, fast swipe should page even though it never
# travelled the 30% of screen width the old distance-only rule demanded.
# That rule is why quick flicks felt like they "didn't take" -- the panel
# sprang back despite an obviously deliberate gesture.
FLICK_VELOCITY_PX_S = 260.0
_VEL_SMOOTHING = 0.6


class TouchState:
    """Shared between the reader thread and the render loop."""

    def __init__(self):
        self.lock = threading.Lock()
        self.active = False
        self.last_activity = time.monotonic()
        self.dx = 0
        self.dy = 0
        self.down_x = 0
        self.down_y = 0
        self.have_pos = False   # a position event has arrived for THIS touch
        self.vx = 0.0           # smoothed horizontal velocity, px/s
        self.release_pending = False
        self.release_dx = 0
        self.release_dy = 0
        self.release_vx = 0.0
        self.error = None       # set if the input device can't be read


touch_state = TouchState()


def _touch_reader():
    import os as _os
    import select
    start_x = start_y = None
    last_x = None
    last_t = 0.0
    down = False
    slot = 0
    try:
        f = open(TOUCH_DEV, "rb", buffering=0)
    except Exception as e:
        # Losing the touch device leaves a dashboard that still redraws
        # happily but ignores every tap. That used to be a single line on
        # stderr nobody reads; surface it on the screen instead so the
        # device doesn't just look wedged.
        with touch_state.lock:
            touch_state.error = "touch input unavailable"
        print(f"touch reader could not open {TOUCH_DEV}: {e}", file=sys.stderr)
        return
    try:
        with f:
            fd = f.fileno()
            _os.set_blocking(fd, False)
            while not _stop:
                select.select([fd], [], [])
                try:
                    data = _os.read(fd, _EVENT_SIZE)
                except (BlockingIOError, TypeError):
                    data = None
                if not data or len(data) < _EVENT_SIZE:
                    time.sleep(0.008)
                    continue
                _, _, typ, code, val = struct.unpack(_EVENT_FMT, data)
                if typ != EV_ABS:
                    continue
                # Multitouch protocol B interleaves slots. Without tracking
                # ABS_MT_SLOT, a second finger's coordinates were folded
                # into the same gesture, which on a screen this small made
                # an accidental two-finger touch jump panels.
                if code == ABS_MT_SLOT:
                    slot = val
                    continue
                if slot != 0:
                    continue
                with touch_state.lock:
                    touch_state.last_activity = time.monotonic()
                if code == ABS_MT_TRACKING_ID:
                    if val == -1 and down:
                        with touch_state.lock:
                            touch_state.active = False
                            touch_state.release_pending = True
                            touch_state.release_dx = touch_state.dx
                            touch_state.release_dy = touch_state.dy
                            touch_state.release_vx = touch_state.vx
                        down = False
                        start_x = start_y = last_x = None
                    else:
                        down = True
                        start_x = start_y = last_x = None
                        with touch_state.lock:
                            touch_state.active = True
                            touch_state.dx = 0
                            touch_state.dy = 0
                            touch_state.vx = 0.0
                            # down_x/down_y still hold the PREVIOUS touch's
                            # coordinates until the first position event of
                            # this one lands; have_pos marks them stale so a
                            # release in that window can't be hit-tested
                            # against where the last tap happened to be.
                            touch_state.have_pos = False
                elif code == ABS_MT_POSITION_X and down:
                    now_t = time.time()
                    if start_x is None:
                        start_x = val
                        with touch_state.lock:
                            touch_state.down_x = val
                            touch_state.have_pos = True
                    elif last_x is not None and now_t > last_t:
                        inst = (val - last_x) / (now_t - last_t)
                        with touch_state.lock:
                            touch_state.vx = (_VEL_SMOOTHING * touch_state.vx
                                              + (1 - _VEL_SMOOTHING) * inst)
                    last_x, last_t = val, now_t
                    with touch_state.lock:
                        touch_state.dx = val - start_x
                elif code == ABS_MT_POSITION_Y and down:
                    if start_y is None:
                        start_y = val
                        with touch_state.lock:
                            touch_state.down_y = val
                    with touch_state.lock:
                        touch_state.dy = val - start_y
    except Exception as e:
        with touch_state.lock:
            touch_state.error = "touch input stopped"
        print(f"touch reader stopped: {e}", file=sys.stderr)


def ease_out_quint(t):
    """Sharper initial move and a longer settle than cubic -- closer to the
    deceleration curve a paged iOS scroll view uses, which is most of why
    that feels "fast but not abrupt"."""
    return 1 - (1 - t) ** 5


def ease_out_cubic(t):
    return 1 - (1 - t) ** 3


def build_strip(cur_img, other_img, other_on_right):
    """Pre-compose the two panels side by side once per drag, so each
    animation frame is a single crop instead of allocating a fresh canvas
    and pasting two full 240x320 images into it. At 60fps on this SoC that
    per-frame allocation was a real slice of the frame budget."""
    strip = Image.new("RGB", (W * 2, H), BG)
    if other_on_right:
        strip.paste(cur_img, (0, 0))
        strip.paste(other_img, (W, 0))
    else:
        strip.paste(other_img, (0, 0))
        strip.paste(cur_img, (W, 0))
    return strip


def strip_frame(strip, dx, other_on_right):
    base = 0 if other_on_right else W
    x = max(0, min(W, base - dx))
    return strip.crop((x, 0, x + W, H))


ANIM_SECONDS = 0.26
ANIM_MIN_SECONDS = 0.14
FRAME_INTERVAL = 1.0 / 60


def flick_duration(distance_px, velocity_px_s):
    """Carry the finger's own speed into the settle animation instead of
    always taking a fixed 0.22s: a hard flick finishes quickly, a slow drag
    released near the threshold eases out gently. Same idea as UIKit
    handing a scroll view its release velocity."""
    v = abs(velocity_px_s)
    if v < 1:
        return ANIM_SECONDS
    return max(ANIM_MIN_SECONDS, min(ANIM_SECONDS, abs(distance_px) / v))


def mode_live():
    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)
    subprocess.run([SCREEN_POWER, "off"], check=True)

    reader = threading.Thread(target=_touch_reader, daemon=True)
    reader.start()

    cfg = load_config()
    if cfg["screen_brightness"] is not None:
        set_screen_brightness(cfg["screen_brightness"])

    # Everything that touches the network or ubus now runs
    # on the refresher's thread. The render loop below only ever reads
    # already-fetched values, so a slow curl or a wedged ubus call can no
    # longer stall drawing or touch handling.
    refresher = Refresher()
    vendor = VendorServices(gl_lua_rpc)
    vendor_visible = None
    vendor_states = {name: empty_vendor(name) for name in VENDOR_TITLES}
    for name in VENDOR_TITLES:
        refresher.add(name, lambda n=name: vendor.snapshot(n), 5,
                      enabled=lambda n=name: vendor_visible == n)
    # 10s: the toggle now shows whether the core is actually running, and
    # OpenClash also gets started/stopped from LuCI or the GL app.
    refresher.add("oc", get_openclash_status, 10)
    refresher.add("traf", get_openclash_traffic_and_node, 20)
    sim_visible = False

    def read_active_sim():
        status = get_sim_status()
        if not sim_visible:
            return None
        cell = _get_active_cell_info(status["slot"]) if status["slot"] is not None else {}
        if not sim_visible:
            return None
        return status, cell

    refresher.add("sim", read_active_sim, SIM_REFRESH_INTERVAL, enabled=lambda: sim_visible)
    link_session = None

    def read_link_health():
        session = link_session
        if session is not None:
            sample = sample_link_health(session["sample"])
            if sample is not None and session is link_session:
                session["sample"] = sample
                return session, sample
        return None

    refresher.add("link", read_link_health, LINK_PROBE_INTERVAL, enabled=lambda: link_session is not None)
    settings_visible = False
    refresher.add("acceleration", lambda: get_network_acceleration() if settings_visible else None, 5)
    refresher.start()

    # Seeded from cache/defaults so the first frame draws immediately
    # instead of waiting on the first refresher pass.
    sim = {"slot": None, "carrier": None, "data_up": False}
    oc = openclash_status_empty(openclash_installed())
    traf = openclash_traffic_empty()

    link_health = None
    cpu_sample, cpu_pct = None, None
    ram_pct, ram_used_gb, ram_total_gb = get_ram_stats()
    temp_c = get_temp_c()
    mon_uptime_min = get_system_info()["uptime_min"]
    last_mon_check = time.time()
    cell_info = _get_active_cell_info()
    conn_type = get_wan_conn_type(cell_info)
    cell_signal = get_cell_signal(cell_info)

    # A short-lived result message over the bottom of the main panels --
    # used when a verified action didn't take, so the toggle snapping back
    # to its real state comes with a reason instead of looking ignored.
    notice = {"text": None, "until": 0.0}
    NOTICE_SECONDS = 5.0
    pending_conflict = None

    def show_notice(text):
        nonlocal pending_conflict
        if text in (ACCELERATION_CONFLICT, OPENCLASH_CONFLICT):
            pending_conflict = text
            return
        notice["text"] = text
        notice["until"] = time.time() + NOTICE_SECONDS

    def render_main(idx):
        img = _render_panel(idx)
        with touch_state.lock:
            err = touch_state.error
        if err:
            d = ImageDraw.Draw(img)
            d.rectangle([0, H - 20, W, H], fill=(90, 26, 34))
            centered_text(d, W / 2, H - 17, tr(err), font("default_medium", 11), (255, 220, 220))
        elif notice["text"] and time.time() < notice["until"]:
            d = ImageDraw.Draw(img)
            f = font("default_medium", 11)
            d.rectangle([0, H - 22, W, H], fill=(84, 62, 18))
            centered_text(d, W / 2, H - 18, truncate_to_width(d, tr(notice["text"]), f, W - 12), f, (255, 230, 180))
        return img

    acceleration = acceleration_empty()
    battery_current = None
    battery_current_due = None

    def _render_panel(idx):
        name = PANEL_NAMES[idx]
        if name == "settings":
            return panel_settings(acceleration, pin_enabled, conn_type, cell_signal)
        elif name == "sim":
            return panel_sim(sim, conn_type, cell_signal, cell_info)
        elif name == "openclash":
            return panel_openclash(oc, traf, conn_type, cell_signal)
        elif name in VENDOR_TITLES:
            return panel_vendor(name, vendor_states[name], conn_type, cell_signal)
        elif name == "battery":
            return panel_battery(conn_type, cell_signal, battery_current)
        else:
            return panel_monitor(link_health, cpu_pct, ram_pct, ram_used_gb, ram_total_gb, temp_c, mon_uptime_min, conn_type, cell_signal)

    PANEL_NAMES[:] = get_modules()
    modules_checked_at = -1e9
    panel_idx = 0
    pin_enabled = screen_pin.enabled()
    view = "pin_keypad" if pin_enabled else "main"
    pin_step = "unlock"
    pin_input = new_pin = pin_notice = ""
    pin_failures = 0
    pin_retry_at = 0.0
    cur_img = panel_pin_keypad("unlock") if pin_enabled else render_main(panel_idx)
    with touch_state.lock:
        touch_state.release_pending = False
        ignore_wake_touch = touch_state.active
    write_frame(cur_img)
    subprocess.run([SCREEN_POWER, "on"], check=True)
    last_draw = time.time()
    last_interaction = last_loop_at = time.monotonic()

    state = "idle"  # idle | dragging | animating  (main-carousel only)
    neighbor_img = None
    neighbor_on_right = True
    drag_strip = None
    last_dx_drawn = None
    anim_from_dx = anim_target_dx = anim_t0 = 0
    anim_seconds = ANIM_SECONDS
    anim_next_idx = panel_idx
    sub_dirty = True

    speedtest = SpeedTest()
    speedtest_frame = {"t": 0.0, "last": None}
    SPEEDTEST_FPS = 12

    def handle_speedtest(now):
        """Owns touch while the speed test screen is up. The test itself
        runs on SpeedTest's thread; this only redraws (at SPEEDTEST_FPS
        while running, on change otherwise) and handles Start/Cancel/back.
        Leaving the screen cancels a running test -- nothing should keep
        burning mobile data behind a screen that's no longer shown."""
        nonlocal view, cur_img, last_draw, sub_dirty
        with touch_state.lock:
            released = touch_state.release_pending
            down_x, down_y = touch_state.down_x, touch_state.down_y
            have_pos = touch_state.have_pos
            release_dx, release_dy = touch_state.release_dx, touch_state.release_dy
            touch_state.release_pending = False

        if released:
            is_tap = have_pos and abs(release_dx) <= TAP_JITTER_PX and abs(release_dy) <= TAP_JITTER_PX
            if (is_tap and hit_back(down_y)) or (not is_tap and release_dx > W * 0.3):
                speedtest.cancel()
                view = "main"
                cur_img = render_main(panel_idx)
                write_frame(cur_img)
                last_draw = now
                return
            if is_tap and hit_speedtest_button(down_x, down_y):
                if speedtest.running:
                    speedtest.cancel()
                else:
                    speedtest.start()
                sub_dirty = True

        snap = speedtest.snapshot()
        running = snap["phase"] in ("download", "upload")
        due = running and now - speedtest_frame["t"] >= 1.0 / SPEEDTEST_FPS
        if sub_dirty or due or (snap != speedtest_frame["last"] and not running):
            write_frame(panel_speedtest(snap, now * SPINNER_SPEED_DPS))
            speedtest_frame["t"] = now
            speedtest_frame["last"] = snap
            sub_dirty = False

    def handle_scroll_picker(now, items, render_fn, on_select, return_view="main"):
        """Shared drag-to-scroll / tap-to-select logic for any scrollable
        picker sub-screen (data cap or proxy node). `items`
        is a list of (key, label); `render_fn(scroll_px)` renders the
        screen; `on_select(key)` applies the choice. Handles going back to
        main on a header tap. Caller still owns sleep+continue."""
        nonlocal view, sub_dirty, cur_img, last_draw, picker_scroll_base
        max_scroll = scroll_picker_max(len(items))
        with touch_state.lock:
            active = touch_state.active
            dy, dx = touch_state.dy, touch_state.dx
            down_x, down_y = touch_state.down_x, touch_state.down_y
            have_pos = touch_state.have_pos
            released = touch_state.release_pending
            release_dx, release_dy = touch_state.release_dx, touch_state.release_dy
            touch_state.release_pending = False

        if active and (abs(dy) > TAP_JITTER_PX or abs(dx) > TAP_JITTER_PX):
            live_scroll = min(max_scroll, max(0, picker_scroll_base - dy))
            write_frame(render_fn(live_scroll))
        elif released:
            final_dx, final_dy = release_dx, release_dy
            is_tap = (have_pos and abs(final_dx) <= TAP_JITTER_PX
                      and abs(final_dy) <= TAP_JITTER_PX)
            if is_tap and hit_back(down_y):
                view = return_view
                sub_dirty = True
                if view == "main":
                    cur_img = render_main(panel_idx)
                    write_frame(cur_img)
                    last_draw = now
            elif is_tap:
                idx = hit_scroll_picker(down_y, len(items), picker_scroll_base)
                if idx is not None:
                    # Hand the picker frame to on_select so a slow apply can
                    # dim *this* screen and spin over it, instead of freezing
                    # on it with no sign of life.
                    on_select(items[idx][0], render_fn(picker_scroll_base))
                    view = return_view
                    sub_dirty = True
                    if view == "main":
                        cur_img = render_main(panel_idx)
                        write_frame(cur_img)
                        last_draw = now
            else:
                picker_scroll_base = min(max_scroll, max(0, picker_scroll_base - final_dy))
                write_frame(render_fn(picker_scroll_base))
        elif sub_dirty:
            write_frame(render_fn(picker_scroll_base))
            sub_dirty = False

    confirm_title = confirm_message = confirm_yes_label = confirm_action = confirm_return_view = ""
    confirm_danger = False
    def is_openclash_action(action):
        return action.startswith("oc_")

    def confirm_accent():
        if confirm_action.startswith("vendor:"):
            return ACCENT[confirm_action.split(":")[1]]
        if is_openclash_action(confirm_action):
            return ACCENT["openclash"]
        return ACCENT["settings"]

    def openclash_confirm():
        nonlocal confirm_title, confirm_message, confirm_yes_label, confirm_action
        nonlocal confirm_return_view, confirm_danger
        want = not openclash_toggle_on(oc)
        confirm_title = "OpenClash"
        if want:
            error = openclash_start_error()
            if error:
                return error
            confirm_message = "Start OpenClash? All traffic will go through the proxy once it's up."
            confirm_yes_label = "Start"
        else:
            confirm_message = "Stop OpenClash? Traffic will go direct, without the proxy."
            confirm_yes_label = "Stop"
        confirm_action = f"oc_toggle:{'on' if want else 'off'}"
        confirm_danger = not want
        confirm_return_view = "main"
        return None

    def run_openclash_action(action):
        """Runs a confirmed OpenClash action under a spinner over the
        OpenClash page and publishes the freshly read state. Returns the
        notice if the action fails."""
        nonlocal oc, traf
        started = time.time()
        base = render_main(PANEL_NAMES.index("openclash"))
        want = action.endswith(":on")
        verb = "Starting" if want else "Stopping"

        def work():
            error = set_openclash_enabled(want)
            ok = False if error else wait_for_openclash_state(want)
            return ok, get_openclash_status(), get_openclash_traffic_and_node(), error

        got = run_with_spinner(base, lambda: f"{verb} OpenClash… {int(time.time() - started)}s",
                               work, ACCENT["openclash"], min_visible=0.8)
        if not got:
            return "Couldn't read OpenClash's state back"
        ok, oc, traf, error = got
        # Publish, so the refresher's older copies can't flip the toggle
        # back on the next pass -- the "turned it off and it jumped back
        # on" report.
        refresher.put("oc", oc)
        refresher.put("traf", traf)
        if error:
            return error
        if ok:
            return None
        limit = int(OPENCLASH_START_TIMEOUT if want else OPENCLASH_STOP_TIMEOUT)
        return (f"OpenClash didn't come up within {limit}s -- check its log in LuCI" if want
                else f"OpenClash still running after {limit}s")

    picker_scroll_base = 0.0
    last_switch_req_check = 0.0
    refresher_seen = -1

    def handle_pin_tap(x, y):
        nonlocal view, pin_step, pin_enabled, pin_input, new_pin, pin_notice
        nonlocal pin_failures, pin_retry_at, sub_dirty, cur_img, last_draw
        if view == "pin_settings":
            if hit_back(y):
                view = "main"
                cur_img = render_main(panel_idx)
                write_frame(cur_img)
                last_draw = time.time()
            elif pin_hit(x, y, PIN_CHANGE_RECT) or (pin_enabled and pin_hit(x, y, PIN_DISABLE_RECT)):
                pin_step = "new"
                if pin_enabled:
                    pin_step = "disable" if pin_hit(x, y, PIN_DISABLE_RECT) else "change"
                pin_input = new_pin = pin_notice = ""
                view = "pin_keypad"
            sub_dirty = True
            return
        if pin_step != "unlock" and hit_back(y):
            pin_input = new_pin = pin_notice = ""
            view = "pin_settings"
            sub_dirty = True
            return
        if time.monotonic() < pin_retry_at:
            return
        key = next((label for label, rect in pin_keys() if pin_hit(x, y, rect)), None)
        if key is None:
            return
        pin_notice = ""
        pin_input = pin_input[:-1] if key == "Del" else pin_input + key
        sub_dirty = True
        if len(pin_input) < 4:
            return
        entered, pin_input = pin_input, ""
        if pin_step in ("unlock", "change", "disable"):
            if not screen_pin.verify(entered):
                pin_failures += 1
                pin_notice = "Incorrect PIN"
                if pin_failures >= 5:
                    pin_retry_at = time.monotonic() + 30
                    pin_notice = "Wait 30 seconds"
                return
            pin_failures = 0
            if pin_step == "unlock":
                Path(SWITCH_REQUEST_FILE).unlink(missing_ok=True)
                view = "main"
                cur_img = render_main(panel_idx)
                write_frame(cur_img)
                last_draw = time.time()
            elif pin_step == "change":
                pin_step = "new"
            else:
                try:
                    screen_pin.disable()
                except OSError:
                    pin_notice = "Could not disable PIN"
                else:
                    pin_enabled = False
                    view = "pin_settings"
        elif pin_step == "new":
            new_pin, pin_step = entered, "confirm"
        else:
            if entered != new_pin:
                pin_notice, pin_step = "PINs did not match", "new"
            else:
                try:
                    screen_pin.save(entered)
                except OSError:
                    pin_notice, pin_step = "Could not save PIN", "new"
                else:
                    pin_enabled = True
                    view = "pin_settings"
            new_pin = ""

    def apply_acceleration(mode=None, base_img=None):
        nonlocal acceleration
        result = run_with_spinner(base_img if base_img is not None else cur_img,
                                  "Applying acceleration…",
                                  lambda: set_network_acceleration(mode=mode), ACCENT["settings"])
        if result:
            acceleration, error = result
            refresher.put("acceleration", acceleration)
            if error:
                show_notice(error)
        else:
            show_notice("Acceleration request failed")
        refresher.request("acceleration")

    def prepare_lock_frame():
        nonlocal pin_input, new_pin, pin_notice, pin_step, view
        nonlocal state, neighbor_img, drag_strip, cur_img, sub_dirty
        speedtest.cancel()
        pin_input = new_pin = ""
        pin_notice = "Wait 30 seconds" if pin_retry_at else ""
        pin_step, view = "unlock", "pin_keypad"
        state, neighbor_img, drag_strip = "idle", None, None
        cur_img = panel_pin_keypad(pin_step, notice=pin_notice)
        write_frame(cur_img)
        sub_dirty = False

    while not _stop:
        now = time.time()
        if time.monotonic() - modules_checked_at >= 1:
            modules_checked_at = time.monotonic()
            visible = get_modules()
            if visible != PANEL_NAMES:
                removed = PANEL_NAMES[panel_idx] not in visible
                panel_idx = selected_index(PANEL_NAMES, panel_idx, visible)
                PANEL_NAMES[:] = visible
                state, neighbor_img, drag_strip = 'idle', None, None
                anim_next_idx = panel_idx
                last_dx_drawn = None
                if removed:
                    speedtest.cancel()
                    if not (view == 'pin_keypad' and pin_step == 'unlock'):
                        view = 'main'
                        pin_input = new_pin = pin_notice = ''
                        pending_conflict = None
                with touch_state.lock:
                    touch_state.release_pending = False
                    ignore_wake_touch = touch_state.active
                last_draw, sub_dirty = 0, True
                if view == 'main':
                    cur_img = render_main(panel_idx)
                    write_frame(cur_img)
        if screen_i18n.refresh_language():
            _fonts.clear()
            _truncate_cache.clear()
            last_draw = 0
            sub_dirty = True
            if view == "main":
                cur_img = render_main(panel_idx)
                write_frame(cur_img)

        screen_asleep = is_screen_asleep()
        monotonic_now = time.monotonic()
        with touch_state.lock:
            last_interaction = max(last_interaction, touch_state.last_activity)
            if touch_state.active and not screen_asleep:
                last_interaction = monotonic_now
        # A slow operation suspends idle timeout until control returns.
        if monotonic_now - last_loop_at > 1:
            last_interaction = monotonic_now
        last_loop_at = monotonic_now
        timeout = cfg["screen_timeout"]
        if not screen_asleep and timeout and monotonic_now - last_interaction >= timeout:
            subprocess.run([SCREEN_POWER, "off"], check=True)
            _asleep_cache["ts"] = 0.0
            screen_asleep = True
            if pin_enabled:
                prepare_lock_frame()
        settings_now = not screen_asleep and (
            (view == "main" and PANEL_NAMES[panel_idx] == "settings") or view == "acceleration_modes")
        if settings_now and not settings_visible:
            refresher.request("acceleration")
        settings_visible = settings_now
        vendor_now = PANEL_NAMES[panel_idx] if not screen_asleep and view == 'main' and PANEL_NAMES[panel_idx] in VENDOR_TITLES else None
        if vendor_now != vendor_visible:
            vendor_visible = vendor_now
            if vendor_visible:
                refresher.request(vendor_visible)
        sim_now = not screen_asleep and view == "main" and PANEL_NAMES[panel_idx] == "sim"
        if sim_now != sim_visible:
            sim_visible = sim_now
            if sim_visible:
                refresher.request("sim")
        monitor_now = not screen_asleep and view == "main" and PANEL_NAMES[panel_idx] == "monitor"
        if monitor_now and link_session is None:
            link_session = {"sample": None}
            refresher.request("link")
        elif not monitor_now and link_session is not None:
            link_session = None
            link_health = None
        if screen_asleep or view != "main" or PANEL_NAMES[panel_idx] != "battery":
            battery_current_due = None
            battery_current = None

        if screen_asleep:
            with touch_state.lock:
                touch_state.release_pending = False
                ignore_wake_touch = touch_state.active
            if pin_enabled and (view != "pin_keypad" or pin_step != "unlock" or pin_input):
                prepare_lock_frame()
            if WAKE_REQUEST.exists():
                WAKE_REQUEST.unlink(missing_ok=True)
                Path(SWITCH_REQUEST_FILE).unlink(missing_ok=True)
                pin_enabled = screen_pin.enabled()
                if pin_enabled:
                    prepare_lock_frame()
                else:
                    pin_input = new_pin = pin_notice = ""
                    view = "main"
                    state, neighbor_img, drag_strip = "idle", None, None
                    cur_img = render_main(panel_idx)
                    write_frame(cur_img)
                subprocess.run([SCREEN_POWER, "on"], check=True)
                _asleep_cache["ts"] = 0.0
                last_interaction = time.monotonic()
                last_draw = now
            time.sleep(0.1)
            continue

        if ignore_wake_touch:
            with touch_state.lock:
                ignore_wake_touch = touch_state.active
                touch_state.release_pending = False
            time.sleep(0.012)
            continue

        # The lock screen owns all input before carousel, settings and
        # physical-button switch requests can reach any other handler.
        if view in ("pin_settings", "pin_keypad"):
            if now - last_switch_req_check > 0.3:
                last_switch_req_check = now
                Path(SWITCH_REQUEST_FILE).unlink(missing_ok=True)
            if pin_retry_at and time.monotonic() >= pin_retry_at:
                pin_retry_at = 0.0
                pin_failures = 0
                pin_notice = ""
                sub_dirty = True
            if sub_dirty:
                img = (panel_pin_settings(pin_enabled) if view == "pin_settings" else
                       panel_pin_keypad(pin_step, len(pin_input), pin_notice))
                write_frame(img)
                sub_dirty = False
            with touch_state.lock:
                tap = (touch_state.release_pending and touch_state.have_pos
                       and abs(touch_state.release_dx) <= TAP_JITTER_PX
                       and abs(touch_state.release_dy) <= TAP_JITTER_PX)
                x, y = touch_state.down_x, touch_state.down_y
                touch_state.release_pending = False
            if tap:
                handle_pin_tap(x, y)
            time.sleep(0.012)
            continue

        if pending_conflict:
            confirm_title, confirm_message = "Conflict", pending_conflict
            confirm_yes_label, confirm_action = "OK", "conflict_ack"
            confirm_return_view, confirm_danger = "main", False
            pending_conflict = None
            view, sub_dirty = "confirm", True

        if now - last_switch_req_check > 0.3:
            last_switch_req_check = now
            if os.path.exists(SWITCH_REQUEST_FILE):
                try:
                    os.remove(SWITCH_REQUEST_FILE)
                except Exception:
                    pass
                confirm_title = "Stock UI"
                confirm_message = "Hand the screen back to the GL.iNet UI?"
                confirm_yes_label = "Switch"
                confirm_danger = False
                confirm_action = "return_stock"
                confirm_return_view = view if view != "confirm" else "main"
                view = "confirm"
                sub_dirty = True

        if view == "main":
            if state == "idle":
                # Pull whatever the background refresher has ready -- only
                # when it has published something new. This used to re-read
                # all eleven values and re-derive the header's connection
                # type/signal on every ~12ms pass (18k refresher reads and
                # 1.6k derivations in a 25s profile) for data that changes
                # every 5-30s. As a side effect, a value this loop set
                # itself (e.g. OpenClash mode right after a tap) is no
                # longer overwritten by the refresher's older copy on the
                # very next pass.
                if refresher.version != refresher_seen:
                    refresher_seen = refresher.version
                    for vendor_name in VENDOR_TITLES:
                        fresh = refresher.get(vendor_name)
                        if fresh and fresh['observed_at'] >= vendor_states[vendor_name]['observed_at']:
                            vendor_states[vendor_name] = fresh
                    oc = refresher.get("oc", oc)
                    traf = refresher.get("traf", traf)
                    acceleration = refresher.get("acceleration", acceleration)
                    sim_sample = refresher.get("sim")
                    if sim_sample is not None:
                        sim, cell_info = sim_sample
                        cell_signal = get_cell_signal(cell_info)
                    link_result = refresher.get("link")
                    if link_result is not None and link_result[0] is link_session:
                        link_health = link_result[1]
                    conn_type = get_wan_conn_type(cell_info)
                if now - last_mon_check > 2:
                    cpu_sample, cpu_pct = sample_cpu(cpu_sample)
                    ram_pct, ram_used_gb, ram_total_gb = get_ram_stats()
                    temp_c = get_temp_c()
                    mon_uptime_min = get_system_info()["uptime_min"]
                    last_mon_check = now
                current_sampled = False
                if PANEL_NAMES[panel_idx] == "battery":
                    sample_time = time.monotonic()
                    if battery_current_due is None or sample_time >= battery_current_due:
                        battery_current = read_battery_current()
                        battery_current_due = sample_time + BATTERY_CURRENT_INTERVAL
                        current_sampled = True
                if current_sampled or now - last_draw >= 1:
                    cur_img = render_main(panel_idx)
                    write_frame(cur_img)
                    last_draw = now

                with touch_state.lock:
                    active = touch_state.active
                    touch_state.release_pending = False
                if active:
                    state = "dragging"

            elif state == "dragging":
                with touch_state.lock:
                    active = touch_state.active
                    dx, dy = touch_state.dx, touch_state.dy
                    down_x, down_y = touch_state.down_x, touch_state.down_y
                    released = touch_state.release_pending
                    release_dx, release_dy = touch_state.release_dx, touch_state.release_dy
                    release_vx = touch_state.release_vx
                    have_pos = touch_state.have_pos
                    touch_state.release_pending = False

                if neighbor_img is None or (neighbor_on_right and dx > TAP_JITTER_PX) or \
                   (not neighbor_on_right and dx < -TAP_JITTER_PX):
                    if dx < 0:
                        neighbor_on_right = True
                        neighbor_img = render_main((panel_idx + 1) % len(PANEL_NAMES))
                        drag_strip = build_strip(cur_img, neighbor_img, True)
                    elif dx > 0:
                        neighbor_on_right = False
                        neighbor_img = render_main((panel_idx - 1) % len(PANEL_NAMES))
                        drag_strip = build_strip(cur_img, neighbor_img, False)
                    last_dx_drawn = None

                if drag_strip is not None:
                    dx_clamped = max(-W, min(W, dx))
                    # The finger only moves so fast; redrawing an identical
                    # offset just burns a frame's worth of RGB565 conversion
                    # that could have gone to the next real one.
                    if dx_clamped != last_dx_drawn:
                        write_frame(strip_frame(drag_strip, dx_clamped, neighbor_on_right))
                        last_dx_drawn = dx_clamped

                if released or not active:
                    final_dx = release_dx if released else dx
                    final_dy = release_dy if released else dy
                    # A release with no position event of its own would be
                    # hit-tested against the PREVIOUS touch's coordinates.
                    is_tap = (have_pos and abs(final_dx) <= TAP_JITTER_PX
                              and abs(final_dy) <= TAP_JITTER_PX)

                    if is_tap:
                        name = PANEL_NAMES[panel_idx]
                        zone = None
                        if name == "settings":
                            zone = hit_settings(down_x, down_y, acceleration)
                        elif name == "openclash":
                            zone = hit_main_openclash(down_x, down_y)
                        elif name in VENDOR_TITLES:
                            zone = "toggle" if hit_vendor_toggle(down_x, down_y) else None
                        elif name == "battery":
                            zone = hit_main_battery(down_x, down_y)
                        elif name == "monitor":
                            zone = hit_main_monitor(down_x, down_y)

                        new_view = None
                        if name in VENDOR_TITLES and zone == 'toggle':
                            fresh = run_with_spinner(cur_img, 'Checking settings…', lambda: vendor.snapshot(name), ACCENT[name])
                            if fresh:
                                vendor_states[name] = fresh
                                refresher.put(name, fresh)
                            if not fresh or not fresh['available']:
                                show_notice('Official service settings are unavailable.')
                            else:
                                want = not fresh['enabled']
                                confirm_title = VENDOR_TITLES[name]
                                confirm_message = ('Start AdGuard Home? DNS settings may change.' if want else 'Stop AdGuard Home? DNS settings may change.') if name == 'adguardhome' else ('Start Tailscale? If login is required, complete it in the WebUI.' if want else 'Stop Tailscale? Remote access may disconnect.')
                                confirm_yes_label = 'Start' if want else 'Stop'
                                confirm_action = 'vendor:' + name + (':on' if want else ':off')
                                confirm_return_view, confirm_danger = 'main', not want
                                new_view = 'confirm'
                        elif name == "battery" and zone:
                            bc, _ = battery_snapshot()
                            if zone == "enable":
                                action, value = "enable", "0" if bc["enabled"] else "1"
                            else:
                                delta = -1 if zone == "minus" else 1
                                action, value = "threshold", max(50, min(100, bc["threshold"] + delta))
                            result = run_with_spinner(cur_img, "Applying charge setting...",
                                                      lambda: battery_action(action, value), ACCENT["battery"])
                            if result:
                                show_notice(result[1])
                        elif name == "settings" and zone == "screen_settings":
                            new_view = "screen_settings"
                        elif name == "settings" and zone == "pin_settings":
                            new_view = "pin_settings"
                        elif name == "settings" and zone in SETTINGS_CONFIRM_ACTIONS:
                            confirm_title, confirm_message, confirm_yes_label = SETTINGS_CONFIRM_ACTIONS[zone]
                            confirm_action, confirm_return_view = zone, "main"
                            confirm_danger = zone in ("reboot", "shutdown")
                            new_view = "confirm"
                        elif name == "settings" and zone == "acceleration":
                            apply_acceleration()
                        elif name == "settings" and zone == "acceleration_mode":
                            acceleration = run_with_spinner(cur_img, "Reading GL settings…", get_network_acceleration, ACCENT["settings"])
                            if acceleration and acceleration["available"]:
                                refresher.put("acceleration", acceleration)
                                if acceleration["enable"]:
                                    picker_scroll_base = 0
                                    new_view = "acceleration_modes"
                            else:
                                acceleration = acceleration or acceleration_empty()
                                show_notice("GL acceleration unavailable")
                        elif name == "openclash" and zone == "toggle":
                            error = run_with_spinner(cur_img, "Checking settings…", openclash_confirm, ACCENT["openclash"])
                            if error:
                                show_notice(error)
                            else:
                                new_view = "confirm"
                        elif name == "openclash" and zone == "mode_global" and oc["mode"] != "global":
                            error = run_with_spinner(cur_img, "Changing mode…",
                                                     lambda: set_openclash_mode("global"), ACCENT["openclash"])
                            if error:
                                show_notice(error)
                            oc = get_openclash_status()
                            refresher.put("oc", oc)
                        elif name == "openclash" and zone == "mode_rule" and oc["mode"] != "rule":
                            error = run_with_spinner(cur_img, "Changing mode…",
                                                     lambda: set_openclash_mode("rule"), ACCENT["openclash"])
                            if error:
                                show_notice(error)
                            oc = get_openclash_status()
                            refresher.put("oc", oc)
                        elif name == "openclash" and zone == "node":
                            picker_scroll_base = 0
                            new_view = "oc_nodes"
                        elif name == "monitor" and zone == "speedtest":
                            new_view = "speedtest"

                        neighbor_img = None
                        state = "idle"
                        if new_view:
                            view = new_view
                            sub_dirty = True
                        else:
                            cur_img = render_main(panel_idx)
                            write_frame(cur_img)
                            last_draw = now
                    else:
                        # Page on EITHER enough travel or enough speed. The
                        # old distance-only rule made a quick flick spring
                        # back, which reads as the gesture being ignored.
                        flick = abs(release_vx) > FLICK_VELOCITY_PX_S
                        same_way = (release_vx < 0) == (final_dx < 0)
                        if abs(final_dx) > W * 0.3 or (flick and same_way and abs(final_dx) > TAP_JITTER_PX):
                            anim_target_dx = -W if final_dx < 0 else W
                            anim_next_idx = (panel_idx + (1 if final_dx < 0 else -1)) % len(PANEL_NAMES)
                        else:
                            anim_target_dx = 0
                            anim_next_idx = panel_idx
                        anim_from_dx = max(-W, min(W, final_dx))
                        anim_seconds = flick_duration(anim_target_dx - anim_from_dx, release_vx)
                        anim_t0 = now
                        last_dx_drawn = None
                        state = "animating"

            elif state == "animating":
                t = (now - anim_t0) / max(0.001, anim_seconds)
                if t >= 1:
                    if anim_next_idx != panel_idx:
                        panel_idx = anim_next_idx
                    cur_img = render_main(panel_idx)
                    write_frame(cur_img)
                    last_draw = now
                    neighbor_img = None
                    drag_strip = None
                    state = "idle"
                else:
                    eased = ease_out_quint(t)
                    dx_now = int(anim_from_dx + (anim_target_dx - anim_from_dx) * eased)
                    if drag_strip is not None and dx_now != last_dx_drawn:
                        write_frame(strip_frame(drag_strip, dx_now, neighbor_on_right))
                        last_dx_drawn = dx_now

        else:  # sub-screen
            if view == "screen_settings":
                if sub_dirty:
                    write_frame(panel_screen_settings(cfg))
                    sub_dirty = False
                with touch_state.lock:
                    tap = (touch_state.release_pending and touch_state.have_pos
                           and abs(touch_state.release_dx) <= TAP_JITTER_PX
                           and abs(touch_state.release_dy) <= TAP_JITTER_PX)
                    x, y = touch_state.down_x, touch_state.down_y
                    touch_state.release_pending = False
                if tap:
                    if hit_back(y):
                        view = "main"
                        cur_img = render_main(panel_idx)
                        write_frame(cur_img)
                        last_draw = now
                    elif 8 <= x <= W - 8 and SETTINGS_TOP <= y < SETTINGS_TOP + 112:
                        view = "screen_brightness" if y < SETTINGS_TOP + 56 else "screen_timeout"
                        picker_scroll_base = 0
                        sub_dirty = True
                time.sleep(0.012)
                continue

            if view in ("screen_brightness", "screen_timeout"):
                brightness_picker = view == "screen_brightness"
                key = "screen_brightness" if brightness_picker else "screen_timeout"
                title = "Brightness" if brightness_picker else "Screen timeout"
                items = ([(value, f"{value}%") for value in range(10, 101, 10)]
                         if brightness_picker else list(SCREEN_TIMEOUTS.items()))
                selected = cfg[key]
                if selected is None:
                    selected = screen_brightness()

                def select_screen_value(value, base_img):
                    try:
                        if brightness_picker:
                            set_screen_brightness(value)
                        new_cfg = dict(cfg, **{key: value})
                        save_config(new_cfg)
                        cfg.update(new_cfg)
                    except (OSError, ValueError):
                        show_notice("Could not save screen setting")

                handle_scroll_picker(now, items,
                                     lambda scroll: panel_scroll_picker(title, ACCENT["settings"], items, selected, scroll),
                                     select_screen_value, return_view="screen_settings")
                time.sleep(0.012)
                continue

            if view == "acceleration_modes":
                acceleration = refresher.get("acceleration", acceleration)
                if not acceleration["available"] or not acceleration["enable"]:
                    view = "main"
                    state = "idle"
                    cur_img = render_main(panel_idx)
                    write_frame(cur_img)
                    last_draw = now
                    continue
                items = [(mode, tr(ACCELERATION_MODES[mode])) for mode in acceleration["list"]]
                handle_scroll_picker(now, items, lambda scroll: panel_acceleration_modes(acceleration, scroll),
                                     lambda mode, img: apply_acceleration(mode, img))
                time.sleep(0.012)
                continue

            if view == "oc_nodes":
                mode = "global" if oc["mode"] == "global" else "rule"
                group, _ = openclash_selector(mode, traf)
                selection = traf["groups"].get(group)
                items = [(name, name) for name in selection["all"]] if selection else []

                def _select_node(key, base_img):
                    nonlocal oc, traf

                    def _apply():
                        status = get_openclash_status()
                        if (status["mode"] == "global") != (mode == "global"):
                            return status, None, "Mode changed. Open the picker again."
                        if mode == "rule" and group != get_rule_group():
                            return status, get_openclash_traffic_and_node(), "Group changed. Open the picker again."
                        if not select_openclash_node(group, key):
                            return status, None, "Could not switch proxy"
                        fresh = get_openclash_traffic_and_node()
                        selected = fresh["groups"].get(group, {}).get("now")
                        error = None if selected == key else "Could not verify selection"
                        return status, fresh, error

                    result = run_with_spinner(base_img, "Switching proxy…", _apply, ACCENT["openclash"])
                    if result:
                        oc, fresh, error = result
                        refresher.put("oc", oc)
                        if fresh is not None:
                            traf = fresh
                            refresher.put("traf", traf)
                        if error:
                            show_notice(error)

                handle_scroll_picker(now, items, lambda s: panel_node_picker(traf, mode, s), _select_node)
                time.sleep(0.012)
                continue

            if view == "speedtest":
                handle_speedtest(now)
                time.sleep(0.012)
                continue

            if sub_dirty:
                if view == "confirm":
                    img = panel_confirm(confirm_title, confirm_message, confirm_accent(),
                                        yes_label=confirm_yes_label, danger=confirm_danger,
                                        acknowledge_only=confirm_action == "conflict_ack")
                else:
                    img = render_sub(view, cfg, oc, traf)
                write_frame(img)
                sub_dirty = False

            with touch_state.lock:
                dx, dy = touch_state.dx, touch_state.dy
                down_x, down_y = touch_state.down_x, touch_state.down_y
                have_pos = touch_state.have_pos
                released = touch_state.release_pending
                release_dx, release_dy = touch_state.release_dx, touch_state.release_dy
                touch_state.release_pending = False

            if released:
                final_dx, final_dy = release_dx, release_dy
                is_tap = (have_pos and abs(final_dx) <= TAP_JITTER_PX
                          and abs(final_dy) <= TAP_JITTER_PX)

                confirm_hit = None
                if view == "confirm" and is_tap:
                    acknowledge_only = confirm_action == "conflict_ack"
                    confirm_hit = hit_confirm(down_x, down_y, acknowledge_only)
                    if not acknowledge_only and hit_back(down_y):
                        confirm_hit = "no"

                if confirm_hit == "no":
                    view = confirm_return_view
                    sub_dirty = True
                    if view == "main":
                        cur_img = render_main(panel_idx)
                        write_frame(cur_img)
                        last_draw = now
                elif confirm_hit == "yes":
                    if confirm_action.startswith('vendor:'):
                        _, name, desired = confirm_action.split(':')
                        outcome = run_with_spinner(cur_img, 'Applying service setting…',
                                                   lambda: vendor.toggle(name, desired == 'on'), ACCENT[name])
                        if outcome:
                            fresh, message = outcome
                            vendor_states[name] = fresh
                            refresher.put(name, fresh)
                            if message in (LOGIN_NOTICE, 'Authorize this device in the Tailscale WebUI.'):
                                confirm_title, confirm_message, confirm_yes_label = 'Tailscale', message, 'OK'
                                confirm_action, confirm_danger = 'conflict_ack', False
                                sub_dirty = True
                            else:
                                if message:
                                    show_notice(message)
                                view = 'main'
                                cur_img = render_main(panel_idx)
                                write_frame(cur_img)
                                last_draw = time.time()
                        else:
                            show_notice('Official service settings are unavailable.')
                            view, sub_dirty = 'main', True
                    elif is_openclash_action(confirm_action):
                        message = run_openclash_action(confirm_action)
                        if message:
                            show_notice(message)
                        view = "main"
                        cur_img = render_main(panel_idx)
                        write_frame(cur_img)
                        last_draw = time.time()
                    elif confirm_action == "conflict_ack":
                        view = "main"
                        cur_img = render_main(panel_idx)
                        write_frame(cur_img)
                        last_draw = now
                    elif confirm_action in ("reboot", "shutdown"):
                        kind = confirm_action
                        (reboot_router if kind == "reboot" else shutdown_router)()
                        t0 = time.time()
                        while time.time() - t0 < POWER_SCREEN_MAX_S and not _stop:
                            elapsed = time.time() - t0
                            write_frame(panel_power(kind, elapsed * SPINNER_SPEED_DPS, elapsed))
                            with touch_state.lock:
                                touch_state.release_pending = False   # nothing to tap now
                            time.sleep(1.0 / 20)
                        if _stop:
                            # procd is taking services down: leave a calm
                            # final frame on the panel and let the loop exit.
                            write_frame(panel_power(kind, final=True))
                        else:
                            show_notice("Reboot didn't start" if kind == "reboot" else "Shutdown didn't start")
                        view = "main"
                    elif confirm_action == "return_stock":
                        # toggle.sh force-kills this very process within
                        # ~1.2s if it hasn't already exited (wait_gone's
                        # poll-then-SIGKILL), then starts gl_screen -- so
                        # the real-world gap is short, but nothing was
                        # ever drawn to the screen during it before, and
                        # a silent frozen dashboard for even a couple of
                        # seconds reads as "did my tap even register?"
                        # Paint an unmistakable "switching" spinner over
                        # the confirm dialog instead, for up to
                        # SWITCH_OVERLAY_MAX_S or until _stop fires
                        # (procd's own TERM has reached us and there's no
                        # point animating any further -- this process is
                        # already being torn down). If the stock UI
                        # somehow still hasn't taken over by the time the
                        # window elapses, falling through to the live
                        # dashboard is an honest fallback rather than
                        # freezing on "switching" forever.
                        base = panel_confirm(confirm_title, confirm_message, ACCENT["settings"],
                                             yes_label=confirm_yes_label, danger=confirm_danger)
                        switch_to_stock_ui()
                        t0 = time.time()
                        while time.time() - t0 < SWITCH_OVERLAY_MAX_S and not _stop:
                            elapsed = time.time() - t0
                            write_frame(draw_loading_overlay(base, "Switching to stock UI…",
                                                             elapsed * SPINNER_SPEED_DPS, ACCENT["settings"]))
                            time.sleep(1.0 / 25)
                        view = "main"
                    sub_dirty = True

                elif is_tap and hit_back(down_y):
                    view = "main"
                    cur_img = render_main(panel_idx)
                    write_frame(cur_img)
                    last_draw = now
                elif not is_tap and final_dx > W * 0.3:
                    # Swiping back used to always land on the main carousel,
                    # even from screens whose header-tap goes somewhere else
                    # Mirror the header-tap destination.
                    parent = confirm_return_view if view == "confirm" else "main"
                    if parent == "main":
                        view = "main"
                        cur_img = render_main(panel_idx)
                        write_frame(cur_img)
                        last_draw = now
                    else:
                        view = parent
                        sub_dirty = True

        time.sleep(0.012)

    sys.exit(0)


if __name__ == "__main__":
    if "--preview" in sys.argv:
        idx = sys.argv.index("--preview")
        outdir = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else "/tmp/mudi7-customizer/preview"
        mode_preview(outdir)
    elif "--calibrate" in sys.argv:
        mode_calibrate()
    else:
        mode_live()
