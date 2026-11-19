from __future__ import annotations

import ipaddress
import re
from typing import Iterable, Optional

from .model import Address


_DIGITS = re.compile(r"\d+")


def iface_name_key(name: str) -> tuple:
    """Sort key matching Batfish's InterfaceNameComparator: split into text and number chunks, numbers
    compare numerically and before text, text ignores case; ties fall back to the raw string."""
    chunks, nxt = [], 0
    for m in _DIGITS.finditer(name):
        chunks.append(name[nxt:m.start()])
        chunks.append(m.group())
        nxt = m.end()
    if nxt < len(name):
        chunks.append(name[nxt:])
    key = tuple((0, int(c)) if c.isdigit() and int(c) < 2**31 else (1, c.lower()) for c in chunks)
    return key, name


def parse_vlan_list(text: str) -> set[int]:
    out: set[int] = set()
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            if a.isdigit() and b.isdigit():
                out.update(range(int(a), int(b) + 1))
        elif part.isdigit():
            out.add(int(part))
    return out


def format_int_space(values: Optional[Iterable[int]]) -> str:
    """Render like Batfish IntegerSpace.toString(): '1-4094' or '10,20,30-40'; '' when empty."""
    if not values:
        return ""
    vals = sorted(set(values))
    ranges, start, prev = [], vals[0], vals[0]
    for v in vals[1:]:
        if v == prev + 1:
            prev = v
            continue
        ranges.append((start, prev))
        start = prev = v
    ranges.append((start, prev))
    return ",".join(str(a) if a == b else f"{a}-{b}" for a, b in ranges)


def parse_address(tokens: list[str]) -> Optional[Address]:
    """Accept ['10.0.0.1/24'] or ['10.0.0.1', '255.255.255.0']."""
    try:
        if "/" in tokens[0]:
            itf = ipaddress.IPv4Interface(tokens[0])
        else:
            itf = ipaddress.IPv4Interface(f"{tokens[0]}/{tokens[1]}")
    except (ValueError, IndexError):
        return None
    return Address(itf.ip, itf.network.prefixlen)


def is_ipv4(text: str) -> bool:
    try:
        ipaddress.IPv4Address(text)
        return True
    except ValueError:
        return False


def is_ipv4_prefix(text: str) -> bool:
    if "/" not in text:
        return False
    try:
        ipaddress.IPv4Network(text, strict=False)
        return True
    except ValueError:
        return False


def as_space_contains(space: Optional[str], asn: Optional[int]) -> bool:
    if space is None or asn is None:
        return False
    for part in space.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            if int(a) <= asn <= int(b):
                return True
        elif part and int(part) == asn:
            return True
    return False


def parse_asn(text: str) -> Optional[int]:
    """Plain or asdot (1.10) notation."""
    try:
        if "." in text:
            hi, lo = text.split(".", 1)
            return int(hi) * 65536 + int(lo)
        return int(text)
    except ValueError:
        return None
