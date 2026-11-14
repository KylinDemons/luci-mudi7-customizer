#!/usr/bin/env python3
"""Generate the resource matching catalog from reviewed source files."""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from stock_i18n import parse_text, placeholders

assets = ROOT / 'stock-i18n'
_, en = parse_text((assets / 'sources/en').read_bytes())
_, zh = parse_text((assets / 'sources/zh-cn').read_bytes())
assert set(en) <= set(zh)
entries = {}
for key, source in en.items():
    translated = zh[key]
    assert placeholders(source) == placeholders(translated), key
    assert source.startswith('"') or source == translated, key
    entries[key] = {'source': source, 'translated': translated}
catalog = {'entries': entries, 'layout': {key: zh[key] for key in zh.keys() - en.keys()}}
path = assets / 'catalog.json'
path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + '\n')
p = assets / 'manifest.json'
manifest = json.loads(p.read_text())
manifest['catalog_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
p.write_text(json.dumps(manifest, indent=2) + '\n')
print('Built resource matching catalog:', len(entries), 'entries')
