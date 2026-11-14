#!/usr/bin/env python3
"""Requires fonttools==4.60.2; see stock-i18n/README.md."""
import re,json,hashlib,zipfile,shutil
from pathlib import Path
from fontTools.ttLib import TTFont
import argparse
parser=argparse.ArgumentParser(description='Build stock UI resources from a read-only device snapshot and IBM Plex Sans SC 1.1.0 zip')
parser.add_argument('device_root',type=Path)
parser.add_argument('font_zip',type=Path)
args=parser.parse_args()
root=Path(__file__).resolve().parents[1]; dev=args.device_root; out=root/'stock-i18n'
payload=out/'payload/etc/gl_screen/language'; payload.mkdir(parents=True,exist_ok=True)
en=dev/'etc/gl_screen/language/text/default'
if en.read_bytes() != (out/'sources/en').read_bytes():
 raise SystemExit('Device language differs from the reviewed source; adapt sources/en and sources/zh-cn first.')
(payload/'text').mkdir(exist_ok=True)
shutil.copyfile(out/'sources/zh-cn',payload/'text/default')
fonts=payload/'ttf'; fonts.mkdir(exist_ok=True)
z=zipfile.ZipFile(args.font_zip); names=z.namelist()
for stem,weight in [('default_medium','Medium'),('default_bold','Bold'),('default_semibold','SemiBold'),('default_mono_medium','Medium'),('default_cn_medium','Medium')]:
 name=next(n for n in names if n.endswith('/fonts/complete/ttf/hinted/IBMPlexSansSC-'+weight+'.ttf'))
 dest=fonts/(stem+'_zh-cn.ttf'); dest.write_bytes(z.read(name)); f=TTFont(dest)
 # Match Latin UI line metrics for every mapping, including CN: avoid the stock 2em line gap.
 ref=TTFont(dev/'etc/gl_screen/language/ttf/default_medium.ttf')
 scale=f['head'].unitsPerEm/ref['head'].unitsPerEm
 for attr in ['ascent','descent','lineGap']: setattr(f['hhea'],attr,round(getattr(ref['hhea'],attr)*scale))
 for attr in ['sTypoAscender','sTypoDescender','sTypoLineGap','usWinAscent','usWinDescent']: setattr(f['OS/2'],attr,round(getattr(ref['OS/2'],attr)*scale))
 # Rename modified fonts to avoid using upstream reserved font family names.
 for rec in f['name'].names:
  if rec.nameID in (1,2,3,4,6,16,17):
   value={'1':'Mudi7 Chinese','2':weight,'3':'Mudi7Chinese-'+weight,'4':'Mudi7 Chinese '+weight,'6':'Mudi7Chinese-'+weight,'16':'Mudi7 Chinese','17':weight}[str(rec.nameID)]
   rec.string=value.encode(rec.getEncoding())
 f.save(dest)
license_name=next(n for n in names if n.endswith('/LICENSE.txt')); (out/'OFL.txt').write_bytes(z.read(license_name))

def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
targets={str(p.relative_to(out/'payload')):sha(p) for p in (out/'payload').rglob('*') if p.is_file()}
guards={str(p.relative_to(dev)):sha(p) for p in (dev/'etc/gl_screen/language/ttf').glob('*.ttf')}; guards['usr/bin/gl_screen']=sha(dev/'usr/bin/gl_screen')
baseline={k:sha(dev/k) if (dev/k).is_file() else None for k in targets}
status=(dev/'usr/lib/opkg/status').read_text()
package=next(block for block in status.split('\n\n') if block.startswith('Package: gl-sdk4-screen-large\n'))
version=next(line.split(': ',1)[1] for line in package.splitlines() if line.startswith('Version: '))
manifest={'identity':{'firmware':(dev/'etc/glversion').read_text().strip(),'screen_package':version},'guards':guards,'baseline':baseline,'targets':targets}
(out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
print('Built',len(targets),'resources',sum(p.stat().st_size for p in fonts.glob('*.ttf')),'font bytes')

# Keep the runtime matching catalog synchronized with reviewed sources.
import subprocess, sys
subprocess.run([sys.executable, str(root / 'tools/build-stock-catalog.py')], check=True)
