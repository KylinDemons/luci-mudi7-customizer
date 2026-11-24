#!/usr/bin/python3
"""Restore access to selected stock GL pages in Simplified Chinese."""
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import zlib

sys.dont_write_bytecode = True
from stock_i18n import atomic

ROOT = Path('/')
BACKUP = Path('/etc/mudi7-gl-ui-unlock')
JOB = Path('/var/run/mudi7-gl-ui-unlock.json')
LOCK = Path('/var/lock/mudi7-gl-ui-unlock.lock')
NGINX = Path('/etc/init.d/nginx')
HOME = 'www/gl_home.html'
INTERNET = 'www/views/gl-sdk4-ui-internet.common.js.gz'
VIEWS = ('vpndashboard', 'vpn-client', 'ovpnserver', 'wgserver', 'dynamicdns', 'adguardhome', 'torview')
MENU_BINDINGS = (
    'firmwareCategory:i,hardwareFeature:n,networkMode:r,lang:o,countryCode:a,hiddenList:s}=e,'
    '{view:l,firmware_categories:c,hardware:u,show_mode:d,hidden_mode:h,lang_hide:f,country:p}=t'
)
MENU_GATE = 'if(null!=f&&f.includes(o))return!1;'
MENU_REGION = 'return!((null==a?void 0:a.toLowerCase().indexOf("cn"))>=0&&null!=f&&f.includes("zh-cn"))'
ICON_REGION = '&&(!t.systemInfo.country_code||t.systemInfo.country_code.toLowerCase().indexOf("cn")<0)'
ICON_LANGUAGE = '"zh-cn"!==t.lang&&'
FIRMWARE_FILES = ('glversion', 'version.build', 'version.date', 'version.type')


class UnlockError(RuntimeError):
    def __init__(self, message, *params):
        super().__init__(message)
        self.params = params


def mismatch(rel):
    return UnlockError('The GL Admin Panel compatibility check failed at %s. Applying the unlock on this firmware may cause problems.', '/' + rel)


def contents(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f'Expected a regular file at {path}')
    return path.read_bytes(), stat.S_IMODE(info.st_mode)


def read_vendor(rel):
    try:
        return contents(ROOT / rel)
    except (OSError, ValueError) as exc:
        raise mismatch(rel) from exc


def target_paths():
    entrypoint, _ = read_vendor(HOME)
    scripts = set(re.findall(rb'(?:src|href)="(/js/app\.[0-9a-f]+\.js)"', entrypoint))
    if len(scripts) != 1:
        raise mismatch(HOME)
    app = 'www' + scripts.pop().decode('ascii') + '.gz'
    return app, INTERNET


def firmware():
    return [read_vendor('etc/' + name)[0].decode('utf-8').strip() for name in FIRMWARE_FILES]


def replace_once(text, original, replacement, rel):
    if text.count(original) != 1:
        raise mismatch(rel)
    return text.replace(original, replacement, 1)


def patch_menu_filter(text, rel):
    # The country check uses lang_hide too, so keep the menu definitions and
    # only make an exception in the language branch for these seven views.
    if text.count(MENU_BINDINGS) != 1 or text.count(MENU_REGION) != 1:
        raise mismatch(rel)
    for view in VIEWS:
        menu = f'usr/share/oui/menu.d/{view}.json'
        try:
            definition = json.loads(read_vendor(menu)[0])
        except ValueError as exc:
            raise mismatch(menu) from exc
        if not isinstance(definition, dict) or definition.get('view') != view or definition.get('lang_hide') != ['zh-cn']:
            raise mismatch(menu)
    allowed = json.dumps(VIEWS, separators=(',', ':'))
    gate = f'if(null!=f&&f.includes(o)&&!("zh-cn"===o&&{allowed}.includes(l)))return!1;'
    return replace_once(text, MENU_GATE, gate, rel)


def patch_icons(text, rel):
    for feature, view in (('adguard', 'adguardhome'), ('vpn', 'vpndashboard'), ('tor', 'torview')):
        condition = f'0===t.networkMode&&t.systemInfo.software_feature.{feature}{ICON_REGION}'
        icon = (
            '?e("li",{class:t.' + feature + 'Status,on:{click:function(e){'
            'return t.$router.push({name:"' + view + '"})}}}'
        )
        text = replace_once(text, ICON_LANGUAGE + condition + icon, condition + icon, rel)
    return text


def plan(paths):
    changes = {}
    for rel, patch in zip(paths, (patch_menu_filter, patch_icons)):
        plain = ROOT / rel.removesuffix('.gz')
        if plain.exists() or plain.is_symlink():
            raise mismatch(str(plain.relative_to(ROOT)))
        original, mode = read_vendor(rel)
        try:
            text = gzip.decompress(original).decode('utf-8')
        except (EOFError, ValueError, OSError, zlib.error) as exc:
            raise mismatch(rel) from exc
        changes[rel] = original, mode, gzip.compress(patch(text, rel).encode('utf-8'), mtime=0)
    return changes


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write_record(path, record):
    atomic(path, json.dumps(record).encode('utf-8'), 0o600)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save_backup(version, changes):
    record = {'firmware': version, 'phase': 'prepared', 'files': {}}
    with tempfile.TemporaryDirectory(prefix='.mudi7-gl-ui-unlock-', dir=BACKUP.parent) as stage:
        stage = Path(stage)
        for rel, (original, mode, patched) in changes.items():
            atomic(stage / Path(rel).name, original, mode)
            record['files'][rel] = {'original': sha(original), 'patched': sha(patched), 'mode': mode}
        write_record(stage / 'record.json', record)
        sync_directory(stage)
        stage.rename(BACKUP)
        sync_directory(BACKUP.parent)
    return record


def restart():
    try:
        subprocess.run([NGINX, 'restart'], check=True, timeout=12, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(10):
            result = subprocess.run([NGINX, 'running'], timeout=2, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if result.returncode == 0:
                return
            time.sleep(0.3)
    except (OSError, subprocess.SubprocessError):
        pass
    raise UnlockError('The GL Admin Panel web server failed to restart.')


def restore():
    if BACKUP.is_symlink():
        raise UnlockError('The GL Admin Panel backup is incomplete or damaged.')
    if not BACKUP.exists():
        raise UnlockError('No GL Admin Panel backup is available.')
    try:
        record = json.loads(read_backup_file(BACKUP / 'record.json'))
        if record['firmware'] != firmware() or set(record['files']) != set(target_paths()):
            raise UnlockError('The GL Admin Panel backup belongs to a different firmware build.')
        originals = {}
        for rel, info in record['files'].items():
            original = read_backup_file(BACKUP / Path(rel).name)
            if sha(original) != info['original'] or type(info['mode']) is not int or not 0 <= info['mode'] <= 0o777:
                raise ValueError('Invalid backup metadata')
            current, mode = read_vendor(rel)
            if sha(current) not in (info['original'], info['patched']):
                raise UnlockError('The GL Admin Panel file %s has changed since the backup was made.', '/' + rel)
            if current != original or mode != info['mode']:
                originals[rel] = original, info['mode']
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise UnlockError('The GL Admin Panel backup is incomplete or damaged.') from exc
    for rel, (original, mode) in originals.items():
        atomic(ROOT / rel, original, mode)
    for rel in record['files']:
        sync_directory((ROOT / rel).parent)
    restart()
    shutil.rmtree(BACKUP)


def read_backup_file(path):
    return contents(path)[0]


def apply():
    if BACKUP.exists() or BACKUP.is_symlink():
        raise UnlockError('Restore the previous GL Admin Panel backup before applying the unlock again.')
    if not os.access(NGINX, os.X_OK):
        raise mismatch(str(NGINX).lstrip('/'))
    version = firmware()
    changes = plan(target_paths())
    record = save_backup(version, changes)
    try:
        for rel, (original, mode, patched) in changes.items():
            if contents(ROOT / rel) != (original, mode):
                raise UnlockError('The GL Admin Panel file %s has changed since the backup was made.', '/' + rel)
            atomic(ROOT / rel, patched, mode)
            sync_directory((ROOT / rel).parent)
        restart()
        record['phase'] = 'applied'
        write_record(BACKUP / 'record.json', record)
    except (OSError, ValueError, UnlockError):
        try:
            restore()
        except (OSError, ValueError, UnlockError) as exc:
            raise UnlockError('The unlock could not be completed. Use Restore changes to retry recovery from the backup.') from exc
        raise


def busy():
    try:
        with LOCK.open('rb') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except FileNotFoundError:
        pass
    except BlockingIOError:
        return True
    return False


def status():
    has_backup = BACKUP.exists() or BACKUP.is_symlink()
    phase = 'original'
    pending = busy()
    if has_backup and not pending:
        phase = 'prepared'
        try:
            record = json.loads(read_backup_file(BACKUP / 'record.json'))
            paths = target_paths()
            if (record['phase'] == 'applied' and record['firmware'] == firmware()
                    and set(record['files']) == set(paths)
                    and all(sha(read_vendor(rel)[0]) == record['files'][rel]['patched'] for rel in paths)):
                phase = 'applied'
        except (OSError, ValueError, KeyError, TypeError, UnlockError):
            pass
    try:
        job = json.loads(read_backup_file(JOB))
    except (OSError, ValueError):
        job = {}
    if not isinstance(job, dict):
        job = {}
    if job.get('pending') and not pending:
        job = {**job, 'pending': False, 'error': 'The GL Admin Panel operation was interrupted. Check its status before trying again.'}
    return {'state': phase, 'can_restore': has_backup, 'pending': pending, 'job': job}


def schedule(action):
    if action not in ('apply', 'restore'):
        raise ValueError('Invalid GL Admin Panel unlock action.')
    with LOCK.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        job_id = uuid.uuid4().hex
        write_record(JOB, {'id': job_id, 'pending': True})
        try:
            subprocess.Popen(
                [sys.executable, __file__, 'worker', action, job_id, str(lock.fileno())],
                pass_fds=(lock.fileno(),), start_new_session=True, close_fds=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError:
            JOB.unlink(missing_ok=True)
            raise
    return job_id


def worker(action, job_id, lock_fd):
    # LuCI is served by the same Nginx instance. Return the RPC job ID
    # before restarting, and hold the inherited lock until completion.
    with os.fdopen(lock_fd, 'a'):
        time.sleep(1)
        job = {'id': job_id, 'pending': False}
        try:
            (apply if action == 'apply' else restore)()
        except UnlockError as exc:
            job.update(error=str(exc), error_params=exc.params)
        except (OSError, ValueError) as exc:
            job.update(error='The GL Admin Panel operation failed at %s.', error_params=[getattr(exc, 'filename', None) or str(exc)])
        write_record(JOB, job)


if __name__ == '__main__':
    if len(sys.argv) == 5 and sys.argv[1] == 'worker' and sys.argv[2] in ('apply', 'restore'):
        worker(sys.argv[2], sys.argv[3], int(sys.argv[4]))
    elif sys.argv[1:] == ['restore-if-needed']:
        with LOCK.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if BACKUP.exists() or BACKUP.is_symlink():
                restore()
    else:
        sys.exit(1)
