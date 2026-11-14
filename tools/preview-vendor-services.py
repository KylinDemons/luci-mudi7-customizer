#!/usr/bin/env python3
"""Render sample service panels without contacting or controlling a service."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import dashboard as d
from vendor_services import empty
from PIL import Image

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('output', type=Path)
parser.add_argument('--language', choices=['zh-cn', 'en'], default='zh-cn')
parser.add_argument('--font-dir', default='/etc/gl_screen/language/ttf')
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
d.screen_i18n._language = args.language
d.FONT_DIR = args.font_dir
d.CHINESE_FONT_DIR = ROOT / 'stock-i18n/payload/etc/gl_screen/language/ttf'
panels = []
for name in ('adguardhome', 'tailscale'):
    base = dict(empty(name), installed=True, available=True)
    samples = [('disabled', dict(base, state='disabled'))]
    if name == 'adguardhome':
        samples += [('running', dict(base, state='running', enabled=True, running=True,
                                     queries=12453, blocked=1352, period_days=1, dns_enabled=True)),
                    ('stats_unavailable', dict(base, state='running', enabled=True, running=True))]
    else:
        samples += [('connected', dict(base, state='connected', enabled=True, running=True,
                                       authenticated=True, address='100.64.0.1')),
                    ('needs_login', dict(base, state='needs_login', enabled=True, running=True)),
                    ('needs_authorization', dict(base, state='needs_authorization', enabled=True, running=True)),
                    ('connecting', dict(base, state='connecting', enabled=True, running=True))]
    for state, sample in samples:
        img = d.panel_vendor(name, sample, '5G', (4, '5G'))
        img.save(args.output / (name + '_' + state + '.png'))
        panels.append(img)
for name in ('adguardhome', 'tailscale'):
    title = d.VENDOR_TITLES[name]
    message = 'Start AdGuard Home? DNS settings may change.' if name == 'adguardhome' else 'Start Tailscale? If login is required, complete it in the WebUI.'
    img = d.panel_confirm(title, message, d.ACCENT[name], yes_label='Start')
    img.save(args.output / (name + '_confirm.png')); panels.append(img)
img = d.panel_confirm('Tailscale', d.LOGIN_NOTICE, d.ACCENT['tailscale'], yes_label='OK', acknowledge_only=True)
img.save(args.output / 'tailscale_login_notice.png'); panels.append(img)
cols=4; rows=(len(panels)+cols-1)//cols
sheet=Image.new('RGB',(cols*250+10,rows*330+10),(30,30,30))
for i,img in enumerate(panels):sheet.paste(img,(10+(i%cols)*250,10+(i//cols)*330))
sheet.save(args.output/'contact_sheet.png')
print('Rendered',len(panels),'service views:',args.output)
