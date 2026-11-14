#!/usr/bin/env python3
"""Read the GL-E5800 RGB565 framebuffer over SSH without waking the display."""
import argparse
from pathlib import Path
import struct
import subprocess
import zlib

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('output', type=Path)
parser.add_argument('--host', default='root@192.168.8.1')
parser.add_argument('--port', type=int, default=22)
args = parser.parse_args()
raw = subprocess.run(['ssh', '-p', str(args.port), '-o', 'BatchMode=yes', args.host,
                      'dd if=/dev/fb0 bs=153600 count=1 2>/dev/null'],
                     check=True, stdout=subprocess.PIPE, timeout=15).stdout
if len(raw) != 240 * 320 * 2:
    raise SystemExit('Unexpected framebuffer length: %d' % len(raw))
rows = []
for y in range(320):
    row = bytearray([0])
    for x in range(240):
        pixel = struct.unpack_from('<H', raw, (y * 240 + x) * 2)[0]
        row.extend((((pixel >> 11) & 31) * 255 // 31,
                    ((pixel >> 5) & 63) * 255 // 63, (pixel & 31) * 255 // 31))
    rows.append(row)


def chunk(kind, data):
    return (struct.pack('>I', len(data)) + kind + data +
            struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff))


args.output.write_bytes(b'\x89PNG\r\n\x1a\n' +
                        chunk(b'IHDR', struct.pack('>IIBBBBB', 240, 320, 8, 2, 0, 0, 0)) +
                        chunk(b'IDAT', zlib.compress(b''.join(rows))) + chunk(b'IEND', b''))
print(args.output)
