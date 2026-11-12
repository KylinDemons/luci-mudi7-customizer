"""Stage the exact GL.iNet Pillow and FreeType packages tested on Mudi 7."""
import hashlib
import io
from pathlib import Path, PurePosixPath
import sys
import tarfile

DEST = Path(sys.argv[1]) / "usr/lib/mudi7-customizer"
DEP_DIR = Path(sys.argv[2])
SOURCES = (
    (
        "python3-pillow_9.5.0-2_aarch64_cortex-a53.ipk",
        "af4bfbb0766923ca55deeb0e6e24e916982247a5b8f2a0f0a41834f24be768ea",
        "usr/lib/python3.11/site-packages",
        "python",
        {"PIL", "Pillow-9.5.0.dist-info"},
    ),
    (
        "libfreetype_2.11.1-1_aarch64_cortex-a53.ipk",
        "cb6973e4f3a7e8bb548281e4e4e196effa7510b4f7b705e718703a38b19f747c",
        "usr/lib",
        "lib",
        {"libfreetype.so.6", "libfreetype.so.6.18.1"},
    ),
)

for filename, digest, prefix, target, roots in SOURCES:
    archive = (DEP_DIR / filename).read_bytes()
    if hashlib.sha256(archive).hexdigest() != digest:
        raise SystemExit(f"Unexpected SHA-256 for {filename}")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as ipk:
        data = ipk.extractfile("./data.tar.gz").read()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as payload:
        for member in payload:
            path = PurePosixPath(member.name)
            if not path.is_relative_to(prefix) or ".." in path.parts:
                continue
            relative = path.relative_to(prefix)
            if not relative.parts or relative.parts[0] not in roots or member.isdir():
                continue
            dest = DEST / target / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            if member.isfile():
                dest.write_bytes(payload.extractfile(member).read())
            elif member.issym() and dest.name == "libfreetype.so.6" and member.linkname == "libfreetype.so.6.18.1":
                dest.symlink_to(member.linkname)
            else:
                raise SystemExit(f"Unexpected archive entry: {member.name}")

for required in (
    "python/PIL/__init__.pyc",
    "python/PIL/_imagingft.cpython-311-aarch64-linux-musl.so",
    "lib/libfreetype.so.6",
):
    if not (DEST / required).is_file():
        raise SystemExit(f"Missing private dependency: {required}")
