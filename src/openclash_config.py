"""The touchscreen's Rule selector setting, shared with LuCI."""
import os
from pathlib import Path

RULE_GROUP_FILE = Path('/etc/mudi7-customizer/openclash-rule-group')
DEFAULT_RULE_GROUP = '默认代理'


def validate_rule_group(name):
    if (not isinstance(name, str) or not name.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in name)
            or len(name.encode('utf-8')) > 256 or name == 'GLOBAL'):
        raise ValueError('请输入实际的 Rule 策略组名称（最多 256 字节，不能使用 GLOBAL）。')
    return name


def get_rule_group():
    try:
        return validate_rule_group(RULE_GROUP_FILE.read_text(encoding='utf-8').removesuffix('\n'))
    except (OSError, ValueError):
        return DEFAULT_RULE_GROUP


def set_rule_group(name):
    name = validate_rule_group(name)
    RULE_GROUP_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = RULE_GROUP_FILE.with_suffix('.new')
    with tmp.open('w', encoding='utf-8') as f:
        f.write(name + '\n')
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(RULE_GROUP_FILE)
