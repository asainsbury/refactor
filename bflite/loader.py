"""Snapshot loading and vendor detection (cf. Batfish VendorConfigurationFormatDetector)."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from .finalize import finalize_node
from .model import Node
from .parsers.cisco import IOS, NXOS, CiscoParser
from .parsers.junos import JunosParser


def detect_vendor(text: str) -> Optional[str]:
    head = text[:20000]
    m = re.search(r"!RANCID-CONTENT-TYPE:\s*(\S+)", head)
    if m:
        t = m.group(1).lower()
        if "nx" in t:
            return NXOS
        if "juniper" in t:
            return "JUNIPER"
        if "cisco" in t:
            return IOS
    lines = [l.strip() for l in head.splitlines() if l.strip()]
    set_lines = sum(1 for l in lines if l.startswith("set "))
    if set_lines > len(lines) / 3 or re.search(r"^\s*(system|interfaces|protocols)\s*\{", head, re.M) \
            or re.search(r"^\s*version\s+\S+;", head, re.M):
        return "JUNIPER"
    if re.search(r"^(feature |switchname |vrf context |boot nxos|!Command: show running-config)", head, re.M) \
            or re.search(r"^\s*vpc domain|^\s*hardware access-list tcam", head, re.M):
        return NXOS
    if re.search(r"^interface \S+", head, re.M) or re.search(r"^hostname \S+", head, re.M):
        return IOS
    return None


def parse_config(text: str, filename: str = "", vendor: Optional[str] = None) -> Optional[Node]:
    vendor = vendor or detect_vendor(text)
    if vendor in (IOS, NXOS):
        return CiscoParser(text, vendor, filename).parse()
    if vendor == "JUNIPER":
        return JunosParser(text, filename).parse()
    return None


def read_config(path: Path) -> str:
    """UTF-8, falling back to Latin-1 for files with other bytes (Batfish shows 'Café', not 'Caf�')."""
    data = path.read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def load_snapshot(path: str | os.PathLike, ignore_mgmt: bool = True) -> dict[str, Node]:
    """Load a Batfish-style snapshot dir (with configs/ sub-folder) or a plain folder of configs.

    Duplicate hostnames: like ParseVendorConfigurationResult.getModifiedNameBase, every node that shares a
    hostname is renamed '<hostname>__<path in the snapshot with / as __>', e.g. 'sw1__configs__sw1-old.cfg'."""
    root = Path(path)
    cfg_dir = root / "configs" if (root / "configs").is_dir() else root
    parsed: list[tuple[str, Node]] = []
    for f in sorted(cfg_dir.rglob("*")):
        if not f.is_file() or f.name.startswith("."):
            continue
        node = parse_config(read_config(f), f.name)
        if node is not None:
            parsed.append((f.relative_to(root).as_posix(), node))
    count: dict[str, int] = {}
    for _, node in parsed:
        count[node.hostname] = count.get(node.hostname, 0) + 1
    nodes: dict[str, Node] = {}
    for rel, node in parsed:
        if count[node.hostname] > 1:
            node.warnings.append(f"duplicate hostname {node.hostname} from {rel}")
            node.hostname = f"{node.hostname}__{rel.replace('/', '__')}"
        nodes[node.hostname] = node
    for n in nodes.values():
        finalize_node(n, ignore_mgmt=ignore_mgmt)
    return nodes
