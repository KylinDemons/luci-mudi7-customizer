"""Monitor 页 Link Health 的 ICMP 探测目标，触屏与 LuCI 共用。

Accepts an IPv4 literal, an IPv6 literal (optionally scoped or bracketed) or a
DNS name -- so the probe can test either address family. Anything that could be
read as a ping option (leading '-'), a URL, a port or a shell fragment is
rejected before it ever reaches the argv of ping. A name is probed over IPv4
(the historical behavior); use an IPv6 literal to test the IPv6 path.
"""
import ipaddress
import os
from pathlib import Path
import re

CONFIG_FILE = Path('/etc/mudi7-customizer/link-probe-host')
DEFAULT_HOST = 'www.baidu.com'
MAX_HOST_LEN = 253
# One DNS label: letters/digits/hyphens, never starting or ending with '-'.
_LABEL = re.compile(r'^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$')
_ZONE = re.compile(r'^[A-Za-z0-9_.-]+$')


def _valid_ipv6(value):
    # Python < 3.9 (and some builds) rejects a scope suffix; validate the
    # address and the zone separately so 'fe80::1%eth0' still works.
    address, _, zone = value.partition('%')
    if '%' in value and (not zone or not _ZONE.match(zone)):
        return False
    try:
        ipaddress.IPv6Address(address)
        return True
    except (ipaddress.AddressValueError, ValueError):
        return False


def host_family(value):
    """'ipv4' / 'ipv6' / 'name' for an already-validated target."""
    try:
        ipaddress.IPv4Address(value)
        return 'ipv4'
    except (ipaddress.AddressValueError, ValueError):
        pass
    return 'ipv6' if _valid_ipv6(value) else 'name'


def validate_host(value):
    if not isinstance(value, str):
        raise ValueError('The probe address must be text.')
    host = value.strip()
    # A trailing dot is a legal fully-qualified form; drop it for storage.
    if host.endswith('.'):
        host = host.rstrip('.')
    # Bracket form is how IPv6 is written in URLs (http://[2400::1]/).
    if host.startswith('[') and host.endswith(']'):
        host = host[1:-1]
    if not host:
        raise ValueError('Enter a probe address, such as 1.1.1.1, 2400:3200::1 or www.baidu.com.')
    if len(host) > MAX_HOST_LEN:
        raise ValueError('Probe address is too long (maximum 253 characters).')

    if '://' in host:
        raise ValueError('Enter only the address, without prefixes such as http://.')
    if host_family(host) in ('ipv4', 'ipv6'):
        return host
    if ':' in host:
        raise ValueError('Invalid IPv6 address. Remove any port number (probes do not use ports).')
    if all(char.isdigit() or char == '.' for char in host):
        # e.g. 192.168.8.999 -- a dotted quad that is not a valid address,
        # otherwise it would be accepted as a numeric hostname.
        raise ValueError('Invalid IPv4 address.')
    if not all(_LABEL.match(label) for label in host.split('.')):
        raise ValueError('Enter a valid domain name (e.g. www.baidu.com), IPv4 address (e.g. 1.1.1.1) or IPv6 address (e.g. 2400:3200::1).')
    return host


def get_probe_host():
    try:
        return validate_host(CONFIG_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return DEFAULT_HOST


def set_probe_host(value):
    host = validate_host(value)
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_name(CONFIG_FILE.name + '.new')
    with tmp.open('w', encoding='utf-8') as f:
        f.write(host + '\n')
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(CONFIG_FILE)
    return host
