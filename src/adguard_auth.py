"""GL.iNet authenticated, loopback-only AdGuard Home statistics client.

Reuse oui.utils.update_ngx_session and the native Admin-Token cookie. Never
reuse browser sessions, log credentials, disable authentication or redirect
credentials to another host. Only two read-only statistics paths are exposed.
"""
import atexit
import http.client
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import time

TOKEN_DIR = Path('/tmp')
TOKEN_PREFIX = 'gl_token_mudi7-customizer-'
REFRESH_LUA = '''local u = require "oui.utils"
assert(type(u.update_ngx_session) == "function")
u.update_ngx_session(TOKEN_PATH)
'''


class AuthenticationError(RuntimeError):
    pass


class StatisticsError(RuntimeError):
    pass


class AdGuardStatistics:
    def __init__(self):
        self.path = TOKEN_DIR / (TOKEN_PREFIX + secrets.token_hex(32))
        self.cookie = 'Admin-Token=' + self.path.name[len('gl_token_'):]
        self.refreshed = -1e9
        atexit.register(self.close)

    def close(self):
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass

    def refresh(self, force=False):
        try:
            if self.path.exists():
                info = self.path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                    raise AuthenticationError('AdGuard Home statistics authentication failed.')
                if not force and time.monotonic() - self.refreshed < 60:
                    return
            else:
                fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                os.close(fd)
            # Let the original vendor helper write its own native token format.
            subprocess.run(['lua', '-'], input=REFRESH_LUA.replace('TOKEN_PATH', json.dumps(str(self.path))), text=True,
                           check=True, timeout=3, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_size < 4:
                self.close()
                raise AuthenticationError('AdGuard Home statistics authentication failed.')
            self.refreshed = time.monotonic()
        except (OSError, subprocess.SubprocessError) as exc:
            self.close()
            raise AuthenticationError('AdGuard Home statistics authentication failed.') from exc

    def _get(self, endpoint):
        if endpoint not in ('stats', 'stats_info'):
            raise ValueError('Unsupported statistics endpoint.')
        connection = http.client.HTTPConnection('127.0.0.1', 3000, timeout=2)
        try:
            connection.request('GET', '/control/' + endpoint,
                               headers={'Cookie': self.cookie, 'Accept': 'application/json'})
            response = connection.getresponse()
            if response.status in (401, 403):
                raise AuthenticationError('AdGuard Home statistics authentication failed.')
            if response.status != 200:
                raise StatisticsError('AdGuard Home statistics are unavailable.')
            data = response.read(4 * 1024 * 1024 + 1)
            if len(data) > 4 * 1024 * 1024:
                raise StatisticsError('AdGuard Home statistics are unavailable.')
            result = json.loads(data)
            if not isinstance(result, dict):
                raise StatisticsError('AdGuard Home statistics are unavailable.')
            return result
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise StatisticsError('AdGuard Home statistics are unavailable.') from exc
        finally:
            connection.close()

    def read(self):
        for attempt in range(2):
            self.refresh(force=bool(attempt))
            try:
                return self._get('stats'), self._get('stats_info')
            except AuthenticationError:
                if attempt:
                    raise
        raise AuthenticationError('AdGuard Home statistics authentication failed.')
