#!/usr/bin/env python3
"""Validate checked-in payload, format placeholders and font mapping."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

root=Path(__file__).resolve().parents[1]/'stock-i18n'
p=json.loads((root/'manifest.json').read_text())
for rel,sha in p['targets'].items():
    assert hashlib.sha256((root/'payload'/rel).read_bytes()).hexdigest()==sha,rel
assert (root/'sources/zh-cn').read_bytes()==(root/'payload/etc/gl_screen/language/text/default').read_bytes()
def parse(text):
    # Official firmware contains this known joined entry.
    text=text.replace('"Tor"STATUS_EXPLANATION_FAST@CHARGING_LABEL_TEXT','"Tor"\nSTATUS_EXPLANATION_FAST@CHARGING_LABEL_TEXT')
    result={}
    for line in text.splitlines():
        if not line.strip() or line.startswith('//'):continue
        key,value=line.split(None,1)
        assert key not in result or result[key] == value, 'Conflicting duplicate key: '+key
        result[key]=value
    return result
en=parse((root/'sources/en').read_text()); zh=parse((root/'sources/zh-cn').read_text())
assert set(en)<=set(zh),set(en)-set(zh)
assert set(zh)-set(en)<= {'SMS_DETAILS_ATTRIBUTE_LABE_TEXT_LINE_SPACE',
    'LOCK_SCREEN_DATA_STYLE1_DAY_LABEL_FORMAT', 'LOCK_SCREEN_DATA_STYLE1_DAY_LABEL_WIDTH',
    'LOCK_SCREEN_DATA_STYLE2_DAY_LABEL_FORMAT', 'LOCK_SCREEN_DATA_STYLE2_DAY_LABEL_X',
    'LOCK_SCREEN_DATA_STYLE2_DAY_LABEL_WIDTH'}
pattern=r'%(?:\d+\$)?[-+0 #]*(?:\d+)?(?:\.\d+)?[a-zA-Z]'
for key in en:
    assert Counter(re.findall(pattern,en[key]))==Counter(re.findall(pattern,zh[key])),key
for key,value in zh.items():
    if key.startswith('FONT_'):
        assert (root/'payload/etc/gl_screen/language/ttf'/(value.strip('"')+'.ttf')).is_file(),key
assert 'tutu' not in (root/'sources/zh-cn').read_text()
print('Verified payload hashes, language keys, printf placeholders and font mappings.')

for style in (1, 2):
    assert re.findall(pattern, zh[f'LOCK_SCREEN_DATA_STYLE{style}_DAY_LABEL_FORMAT']) == ['%s', '%d']

assert hashlib.sha256((root/'catalog.json').read_bytes()).hexdigest() == p['catalog_sha256']
catalog = json.loads((root/'catalog.json').read_text())
assert set(catalog['entries']) == set(en)
for key, entry in catalog['entries'].items():
    assert entry == {'source': en[key], 'translated': zh[key]}, key
assert catalog['layout'] == {key: zh[key] for key in zh.keys() - en.keys()}
print('Verified resource matching catalog and source synchronization.')
