"""Display/control adapters for GL.iNet's official services.

Only set_config changes state. Reads never request auth URLs, log out, alter
DNS or call tailscale up/down. User settings are read immediately before a
write and carried through in full.
"""
import json
from pathlib import Path
import subprocess
import threading
import time
from adguard_auth import AdGuardStatistics, AuthenticationError, StatisticsError

NAMES = {'adguardhome': 'AdGuard Home', 'tailscale': 'Tailscale'}
RPC_DIR = Path('/usr/lib/oui-httpd/rpc')
LOGIN_NOTICE = 'Log in to Tailscale in the WebUI before using it here.'


def json_command(argv, timeout=5):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return json.loads(result.stdout) if result.stdout.strip() else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def process_running(name):
    data = json_command(['ubus', 'call', 'service', 'list', json.dumps({'name': name})])
    instances = data.get(name, {}).get('instances', {}) if isinstance(data, dict) else {}
    return any(instance.get('running') is True for instance in instances.values())


def empty(name):
    return {'name': name, 'installed': False, 'available': False, 'enabled': False,
            'running': False, 'state': 'unavailable', 'queries': None, 'blocked': None,
            'period_days': None, 'stats_error': '', 'dns_enabled': None, 'authenticated': False,
            'address': None, 'notice': '', 'observed_at': time.monotonic()}


class VendorServices:
    def __init__(self, rpc):
        self.rpc = rpc
        self.lock = threading.RLock()
        self.statistics = AdGuardStatistics()

    def _config(self, name):
        try:
            ok, config = self.rpc(str(RPC_DIR / name), 'get_config', {}, timeout=8)
        except (OSError, subprocess.SubprocessError):
            return None
        if not ok or not isinstance(config, dict) or type(config.get('enabled')) is not bool:
            return None
        return config

    def snapshot(self, name):
        if name not in NAMES:
            raise ValueError('Unknown official service.')
        with self.lock:
            state = empty(name)
            state['installed'] = (RPC_DIR / name).is_file()
            if not state['installed']:
                state['state'] = 'not_installed'
                return state
            config = self._config(name)
            if config is None:
                return state
            state.update(available=True, enabled=config['enabled'], running=process_running(name))
            state['state'] = 'running' if state['enabled'] and state['running'] else 'inactive' if state['enabled'] else 'running_disabled' if state['running'] else 'disabled'
            if name == 'adguardhome':
                state['dns_enabled'] = config.get('dns_enabled')
                if state['running']:
                    try:
                        stats, info = self.statistics.read()
                    except AuthenticationError:
                        stats, info = None, None
                        state['stats_error'] = 'Statistics authentication failed'
                    except StatisticsError:
                        stats, info = None, None
                        state['stats_error'] = 'Statistics unavailable'
                    if isinstance(stats, dict):
                        for source, target in [('num_dns_queries', 'queries'), ('num_blocked_filtering', 'blocked')]:
                            value = stats.get(source)
                            if type(value) is int and value >= 0:
                                state[target] = value
                    if isinstance(info, dict) and type(info.get('interval')) is int:
                        state['period_days'] = info['interval']
            elif state['running']:
                actual = json_command(['/usr/sbin/tailscale', 'status', '--json'], timeout=4)
                if not isinstance(actual, dict):
                    state['state'] = 'unavailable'
                else:
                    backend = actual.get('BackendState')
                    me = actual.get('Self') if isinstance(actual.get('Self'), dict) else {}
                    state['authenticated'] = backend not in ('NeedsLogin', 'NeedsMachineAuth', 'NoState', None) and bool(me.get('UserID'))
                    addresses = actual.get('TailscaleIPs') if isinstance(actual.get('TailscaleIPs'), list) else []
                    state['address'] = next((ip for ip in addresses if isinstance(ip, str) and ':' not in ip), None)
                    if backend not in ('NeedsLogin', 'NeedsMachineAuth', 'Running', 'Starting', 'NoState', 'InUseOtherUser', 'Stopped'):
                        state['state'] = 'unavailable'
                    elif backend == 'NeedsLogin':
                        state['state'], state['notice'] = 'needs_login', LOGIN_NOTICE
                    elif backend == 'NeedsMachineAuth':
                        state['state'] = 'needs_authorization'
                    elif state['enabled']:
                        state['state'] = 'connected' if backend == 'Running' and me.get('Online') is True else 'connecting'
            state['observed_at'] = time.monotonic()
            return state

    def toggle(self, name, desired):
        if name not in NAMES or type(desired) is not bool:
            raise ValueError('Invalid official service operation.')
        with self.lock:
            before = self.snapshot(name)
            if not before['available']:
                return before, 'Official service settings are unavailable.'
            if before['enabled'] == desired:
                reached = (not desired and not before['running']) or (desired and before['running'] and (name == 'adguardhome' or before['state'] == 'connected'))
                return before, before['notice'] or (None if reached else 'The service has not reached the requested state. Check its status in the WebUI.')
            # A fresh configuration prevents a toggle from erasing other options.
            config = self._config(name)
            if config is None:
                return before, 'Official service settings are unavailable.'
            fields = ['dns_enabled'] if name == 'adguardhome' else ['lan_enabled', 'wan_enabled', 'masq', 'run_exit_node']
            if any(type(config.get(key)) is not bool for key in fields):
                return before, 'Unsupported official service settings. Check for plugin updates.'
            params = {key: config[key] for key in fields}
            params['enabled'] = desired
            if name == 'tailscale':
                params['exit_node_ip'] = config.get('exit_node_ip') or ''
            try:
                ok, result = self.rpc(str(RPC_DIR / name), 'set_config', params, timeout=75)
            except (OSError, subprocess.SubprocessError):
                return self.snapshot(name), 'Official service operation timed out. Check its actual status.'
            # Check errors even if a transport adapter treats err_code=0 as success.
            if not ok or isinstance(result, dict) and (result.get('err_msg') or result.get('err_code', 0)):
                return self.snapshot(name), 'The official service rejected the change. Check its settings in the WebUI.'
            deadline = time.monotonic() + 25
            while True:
                state = self.snapshot(name)
                if state['available'] and state['enabled'] == desired:
                    if not desired and not state['running']:
                        return state, None
                    if desired and state['running']:
                        if name == 'adguardhome' or state['state'] == 'connected':
                            return state, None
                        if state['state'] == 'needs_login':
                            return state, LOGIN_NOTICE
                        if state['state'] == 'needs_authorization':
                            return state, 'Authorize this device in the Tailscale WebUI.'
                if time.monotonic() >= deadline:
                    return state, 'The service has not reached the requested state. Check its status in the WebUI.'
                time.sleep(1)
