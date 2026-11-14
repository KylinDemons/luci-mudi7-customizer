#!/usr/bin/python3
"""Resource-matched, reversible stock screen localization."""
import fcntl
import hashlib
import json
import os
import re
from collections import Counter
from pathlib import Path
import shutil
import subprocess
import tempfile

ASSETS = Path('/usr/share/mudi7-customizer/stock-i18n')
BACKUP = Path('/etc/mudi7-stock-i18n')
ROOT = Path('/')
LOCK = Path('/var/lock/mudi7-stock-i18n.lock')


def digest(path):
    if path.is_symlink():
        raise RuntimeError('Unexpected symbolic link in stock screen resources.')
    if not path.is_file():
        return None
    sha = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            sha.update(chunk)
    return sha.hexdigest()


def load(path):
    return json.loads(path.read_text())


def atomic(path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.stock-i18n-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def identity():
    status = (ROOT / 'usr/lib/opkg/status').read_text()
    version = None
    for block in status.split('\n\n'):
        fields = dict(line.split(': ', 1) for line in block.splitlines() if ': ' in line)
        if fields.get('Package') == 'gl-sdk4-screen-large' and fields.get('Status', '').split()[-1:] == ['installed']:
            version = fields.get('Version')
    return {'firmware': (ROOT / 'etc/glversion').read_text().strip(), 'screen_package': version}


def profile():
    p = load(ASSETS / 'manifest.json')
    return p


TEXT = 'etc/gl_screen/language/text/default'
WARNING = 'This screen version or some language entries are not fully verified. Problems may occur. If they do, restore changes and wait for adaptation.'
KEY = re.compile(r'^([A-Za-z0-9_@]+)\s+(.+)$')
PLACEHOLDER = re.compile(r'%(?:\d+\$)?[-+0 #]*(?:\d+|\*)?(?:\.(?:\d+|\*))?[hlLjzt]*[diuoxXfFeEgGaAcspn]')


def parse_text(data):
    text = data.decode('utf-8')
    if len(data) > 2 * 1024 * 1024 or '\x00' in text:
        raise ValueError('Invalid stock UI language resource.')
    # Known upstream missing newline; split it before matching either entry.
    text = text.replace('"Tor"STATUS_EXPLANATION_FAST@CHARGING_LABEL_TEXT',
                        '"Tor"\nSTATUS_EXPLANATION_FAST@CHARGING_LABEL_TEXT')
    lines, entries = [], {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith('//'):
            lines.append((None, line))
            continue
        match = KEY.fullmatch(line)
        if not match:
            raise ValueError('Invalid stock UI language resource.')
        key, value = match.groups()
        value = value.rstrip()
        if value.startswith('"') and not value.endswith('"'):
            raise ValueError('Invalid stock UI language resource.')
        if key in entries and entries[key] != value:
            raise ValueError('Conflicting stock UI language entries.')
        entries[key] = value
        lines.append((key, value))
    if not entries:
        raise ValueError('Invalid stock UI language resource.')
    return lines, entries


def placeholders(value):
    return Counter(PLACEHOLDER.findall(value.replace('%%', '')))


def resource_guards(p):
    paths = set(p['guards']) | {'rom/' + TEXT}
    return {rel: digest(ROOT / rel) for rel in sorted(paths)}


def same_environment(b, p):
    if 'resource_guards' in b:
        return all(digest(ROOT / rel) == sha for rel, sha in b['resource_guards'].items())
    # Compatibility with backups made by plugin 1.0.1/1.0.2.
    return (b['identity'] == identity() and
            all(digest(ROOT / rel) == sha for rel, sha in p['guards'].items()))


def backup_record(p):
    if BACKUP.is_symlink():
        raise RuntimeError('Unexpected symbolic link in stock screen resources.')
    if not BACKUP.exists():
        return None
    try:
        b = load(BACKUP / 'record.json')
        if set(b['originals']) != set(p['targets']) or set(b['applied']) != set(p['targets']):
            raise ValueError('targets')
        # Guard names are never accepted as restoration destinations.
        if 'resource_guards' in b and set(b['resource_guards']) != set(p['guards']) | {'rom/' + TEXT}:
            raise ValueError('guard paths')
        for rel, entry in b['originals'].items():
            if entry['sha256'] is not None and digest(BACKUP / 'files' / rel) != entry['sha256']:
                raise ValueError('backup checksum')
        return b
    except (OSError, ValueError, KeyError, TypeError):
        raise RuntimeError('The stock UI backup is damaged or belongs to another resource layout. Keep it for recovery.')


def original_bytes(p, b):
    if b:
        return (BACKUP / 'files' / TEXT).read_bytes()
    current = digest(ROOT / TEXT)
    rom = digest(ROOT / ('rom/' + TEXT))
    # Establish provenance, not just parseability: do not overwrite another mod.
    if current is None or not (current == rom or current == p['baseline'][TEXT]):
        raise RuntimeError('Stock UI resources have unknown changes. Restore the previous backup or original firmware resources first.')
    if any(digest(ROOT / rel) is not None for rel in p['targets'] if rel != TEXT):
        raise RuntimeError('Stock UI resources have unknown changes. Restore the previous backup or original firmware resources first.')
    return (ROOT / TEXT).read_bytes()


def plan(p, b=None):
    if (ROOT / 'proc/gl-hw-info/model').read_text().strip() != 'e5800' or not identity()['screen_package']:
        raise RuntimeError('The stock UI resource format or device is not supported. Check for plugin updates.')
    if digest(ROOT / 'usr/bin/gl_screen') is None:
        raise RuntimeError('The stock UI resource format or device is not supported. Check for plugin updates.')
    if digest(ASSETS / 'catalog.json') != p['catalog_sha256']:
        raise RuntimeError('The bundled stock UI language pack is damaged.')
    catalog = load(ASSETS / 'catalog.json')
    lines, entries = parse_text(original_bytes(p, b))
    if 'FONT_MEDIUM' not in entries:
        raise RuntimeError('The stock UI resource format or device is not supported. Check for plugin updates.')
    translated, unmatched = {}, 0
    for key, value in entries.items():
        known = catalog['entries'].get(key)
        if known and known['source'] == value:
            replacement = known['translated']
            if placeholders(value) != placeholders(replacement):
                raise RuntimeError('The bundled stock UI language pack is damaged.')
            if not value.startswith('"') and replacement != value:
                raise RuntimeError('The bundled stock UI language pack is damaged.')
            translated[key] = replacement
        else:
            translated[key] = value  # Preserve unknown/changed entries verbatim.
            unmatched += 1
        if key.startswith('FONT_'):
            if not known or known['source'] != value:
                raise RuntimeError('The stock UI font mapping is not supported. Check for plugin updates.')
            basename = translated[key].strip('"')
            rel = 'etc/gl_screen/language/ttf/' + basename + '.ttf'
            if rel not in p['targets']:
                raise RuntimeError('The bundled stock UI language pack is damaged.')
    verified_layout = all(digest(ROOT / rel) == sha for rel, sha in p['guards'].items())
    output = [line if key is None else key + ' ' + translated[key] for key, line in lines]
    if verified_layout:
        # Apply only overrides whose containing base keys still match this source.
        # Unknown implementations receive translation only, preserving layout.
        for key, value in catalog['layout'].items():
            if key not in entries:
                output.append(key + ' ' + value)
    text = ('\n'.join(output) + '\n').encode('utf-8')
    return {'text': text, 'warning': WARNING if unmatched or not verified_layout else '',
            'untranslated': unmatched, 'matched': len(entries) - unmatched,
            'layout_applied': verified_layout,
            'compatibility': 'verified' if verified_layout and not unmatched else 'partial' if unmatched else 'unverified'}


def status():
    try:
        p = profile()
        b = backup_record(p)
        safe_restore = b is not None and same_environment(b, p)
        current = {rel: digest(ROOT / rel) for rel in p['targets']}
        applied = safe_restore and current == b['applied'] and b['phase'] == 'applied'
        try:
            result = plan(p, b)
            supported, reason = True, ''
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            result = {}
            supported, reason = False, str(exc)
        return {'state': 'applied' if applied else 'original' if supported and b is None else 'unknown',
                'supported': supported, 'can_restore': safe_restore,
                'warning': result.get('warning', ''), 'reason': reason,
                'matched': result.get('matched', 0), 'untranslated': result.get('untranslated', 0),
                'layout_applied': result.get('layout_applied', False),
                'compatibility': result.get('compatibility', 'unsupported'),
                **identity()}
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        return {'state': 'unknown', 'supported': False, 'can_restore': False, 'reason': str(exc)}


def running():
    return subprocess.run(['/etc/init.d/gl_screen', 'running'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def restart(active):
    # Do not start stock UI while the third-party screen owns the framebuffer.
    if active:
        subprocess.run(['/etc/init.d/gl_screen', 'restart'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        import time
        time.sleep(2)
        if not running():
            raise RuntimeError('The stock UI failed to restart.')


def restore_files(b):
    for rel, entry in b['originals'].items():
        path = ROOT / rel
        if path.is_symlink():
            raise RuntimeError('Unexpected symbolic link in stock screen resources.')
        if entry['sha256'] is None:
            path.unlink(missing_ok=True)
        else:
            atomic(path, (BACKUP / 'files' / rel).read_bytes(), entry['mode'])
    if any(digest(ROOT / rel) != entry['sha256'] for rel, entry in b['originals'].items()):
        raise RuntimeError('Stock UI restoration verification failed.')


def operate(action):
    with LOCK.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        p = profile()
        b = backup_record(p)
        if action == 'restore':
            if b is None:
                raise RuntimeError('No stock UI backup is available.')
            if not same_environment(b, p):
                raise RuntimeError('The screen resources changed since the backup was made. Refusing to restore across incompatible screen versions.')
            active = running()
            restore_files(b)
            restart(active)
            shutil.rmtree(BACKUP)
            return status()
        if action != 'apply':
            raise ValueError('Invalid stock UI action.')
        if status()['state'] == 'applied':
            return status()
        if b is not None:
            raise RuntimeError('Stock UI resources have unknown changes. Restore the previous backup or original firmware resources first.')
        result = plan(p)
        for rel, sha in p['targets'].items():
            if digest(ASSETS / 'payload' / rel) != sha:
                raise RuntimeError('The bundled stock UI language pack is damaged.')
        payload = {rel: (result['text'] if rel == TEXT else (ASSETS / 'payload' / rel).read_bytes())
                   for rel in p['targets']}
        applied_hashes = {rel: hashlib.sha256(data).hexdigest() for rel, data in payload.items()}
        active = running()
        temp = Path(tempfile.mkdtemp(prefix='.mudi7-stock-i18n-', dir=str(BACKUP.parent)))
        try:
            b = {'identity': identity(), 'originals': {}, 'applied': applied_hashes, 'phase': 'prepared',
                 'resource_guards': resource_guards(p)}
            for rel in p['targets']:
                path = ROOT / rel
                sha = digest(path)
                b['originals'][rel] = {'sha256': sha, 'mode': path.stat().st_mode & 0o777 if sha is not None else 0o644}
                if sha is not None:
                    dest = temp / 'files' / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, dest)
            atomic(temp / 'record.json', json.dumps(b).encode(), 0o600)
            os.chmod(temp, 0o700)
            os.rename(temp, BACKUP)
        finally:
            if temp.exists():
                shutil.rmtree(temp)
        try:
            for rel in p['targets']:
                atomic(ROOT / rel, payload[rel])
            if any(digest(ROOT / rel) != sha for rel, sha in applied_hashes.items()):
                raise RuntimeError('Stock UI language pack verification failed.')
            restart(active)
            b['phase'] = 'applied'
            atomic(BACKUP / 'record.json', json.dumps(b).encode(), 0o600)
        except Exception:
            restore_files(b)
            restart(active)
            shutil.rmtree(BACKUP)
            raise
        return status()


if __name__ == '__main__':
    import sys
    print(json.dumps(status() if sys.argv[1:] == ['status'] else operate(sys.argv[1])))
