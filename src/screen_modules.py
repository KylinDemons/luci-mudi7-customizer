"""Visible third-party screen pages; hiding a page never stops its service."""
import json
import os
from pathlib import Path

MODULES = ('sim', 'monitor', 'battery', 'openclash', 'adguardhome', 'tailscale', 'settings')
CONFIG_FILE = Path('/etc/mudi7-customizer/screen-modules.json')


def validate_modules(value):
    if not isinstance(value, list) or not value:
        raise ValueError('Select at least one screen module.')
    if any(not isinstance(name, str) or name not in MODULES for name in value) or len(set(value)) != len(value):
        raise ValueError('Invalid screen module selection.')
    return [name for name in MODULES if name in value]


def get_modules():
    try:
        return validate_modules(json.loads(CONFIG_FILE.read_text()))
    except (OSError, ValueError, TypeError):
        return list(MODULES)


def set_modules(value):
    modules = validate_modules(value)
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = CONFIG_FILE.with_suffix('.new')
    with temp.open('w') as stream:
        json.dump(modules, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(CONFIG_FILE)
    return modules


def selected_index(old_names, old_index, new_names):
    """Keep the current page by ID; fall back to the first visible page."""
    current = old_names[old_index]
    return new_names.index(current) if current in new_names else 0
