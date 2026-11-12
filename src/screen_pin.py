"""Local PIN storage for the Dashboard touchscreen."""
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import tempfile

PIN_FILE = Path("/etc/mudi7-customizer/screen-pin")
PIN_PATTERN = re.compile(r"[0-9]{4}")


def enabled():
    # A malformed record still locks the screen.
    try:
        PIN_FILE.stat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def digest(pin, salt):
    return hashlib.md5(salt + pin.encode("ascii"), usedforsecurity=False).hexdigest()


def verify(pin):
    if not PIN_PATTERN.fullmatch(pin):
        return False
    try:
        salt_hex, expected = PIN_FILE.read_text(encoding="ascii").strip().split(":")
        salt = bytes.fromhex(salt_hex)
        return len(salt) == 16 and hmac.compare_digest(digest(pin, salt), expected)
    except (OSError, ValueError):
        return False


def save(pin):
    if not PIN_PATTERN.fullmatch(pin):
        raise ValueError("Enter exactly four digits")
    salt = secrets.token_bytes(16)
    PIN_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=PIN_FILE.parent, prefix=".screen-pin-")
    try:
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(f"{salt.hex()}:{digest(pin, salt)}\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, PIN_FILE)
    finally:
        Path(name).unlink(missing_ok=True)


def disable():
    PIN_FILE.unlink(missing_ok=True)
