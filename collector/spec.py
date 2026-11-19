"""What the production pipeline collects (read from the pipeline README, not included here).

Every answer is cast to string and written as JSON lines (pandas: df.astype(str).to_json(orient="records",
lines=True)) under <out>/<folder>/<prefix><sep><utc>.json, with <utc> = datetime.now(UTC) as %Y%m%d_%H%M.
Questions are called with no node or property filters.

Quirks kept verbatim from the production scripts:
  * node_property<utc>.json and vlan_property<utc>.json have no underscore before the timestamp.
  * ipOwners is the only answer written with a fixed column order (IP, Node, VRF, Interface, Mask, Active)
    and sorted by IP.
  * fileParseStatus is not collected; only parseWarning goes into parse_status/.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

# (pybatfish question, output folder, file prefix, separator before timestamp, production script)
QUESTIONS = [
    ("initIssues", "init_issue", "init_issue", "_", "batfish_snapshot.py"),
    ("parseWarning", "parse_status", "parse_warning", "_", "batfish_snapshot.py"),
    ("interfaceProperties", "interface_property", "interface_property", "_", "batfish_interface_property.py"),
    ("nodeProperties", "node_property", "node_property", "", "batfish_node_property.py"),
    ("ipOwners", "ip_owner", "ip_owner", "_", "batfish_node_property.py"),
    ("switchedVlanProperties", "vlan_property", "vlan_property", "", "batfish_node_property.py"),
    ("bgpPeerConfiguration", "bgp_peer_configurations", "bgp_peer_configuration", "_", "batfish_bgp_peer_config.py"),
    ("bgpSessionCompatibility", "bgp_session_compatibility", "bgp_session_compatibility", "_", "batfish_bgp_peer_config.py"),
    ("bgpSessionStatus", "bgp_session_status", "bgp_session_status", "_", "batfish_bgp_peer_config.py"),
    ("routes", "routes", "routes", "_", "batfish_routes.py"),
]

IP_OWNERS_COLUMNS = ["IP", "Node", "VRF", "Interface", "Mask", "Active"]

# Columns that identify a row, used by tools/compare.py to line rows up across the two outputs.
ROW_KEYS = {
    "initIssues": None, "parseWarning": None,
    "interfaceProperties": ["Interface"],
    "nodeProperties": ["Node"],
    "ipOwners": ["Node", "Interface", "IP"],
    "switchedVlanProperties": ["Node", "VLAN_ID"],
    "bgpPeerConfiguration": ["Node", "VRF", "Remote_IP", "Local_Interface"],
    "bgpSessionCompatibility": ["Node", "VRF", "Remote_IP", "Local_Interface"],
    "bgpSessionStatus": ["Node", "VRF", "Remote_IP", "Local_Interface"],
    "routes": ["Node", "VRF", "Network", "Next_Hop"],
}


def utc_stamp(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y%m%d_%H%M")


def out_path(root: Path, question: str, stamp: str) -> Path:
    for q, folder, prefix, sep, _ in QUESTIONS:
        if q == question:
            return root / folder / f"{prefix}{sep}{stamp}.json"
    raise KeyError(question)


_PD_STR = None


def _pd_str(v) -> str:
    """Same text pandas' astype(str) gives an object cell: str() of the value (None -> 'None'), and
    list cells formatted like pybatfish's ListWrapper (pandas pprint_thing). Imported lazily because the
    reference collector also loads this module inside the Batfish container, where bflite isn't installed."""
    global _PD_STR
    if _PD_STR is None:
        from bflite.datamodel import pd_str
        _PD_STR = pd_str
    return _PD_STR(v)


def write_jsonl(path: Path, columns: list[str], rows: Iterable[dict]) -> int:
    """Byte-compatible with df.astype(str).to_json(path, orient='records', lines=True) under pandas < 3:
    every value a string (missing values as "None"), '/' escaped as '\\/', ASCII only, one record per
    line, trailing newline. pandas 3 writes missing values as null instead; tools/compare.py treats the two
    as equal.

    Each line is assembled from json's own C string encoder (the one JSONEncoder(ensure_ascii=True) uses),
    which is about twice as fast as encoding a dict per row and gives the same bytes."""
    from json.encoder import encode_basestring_ascii as esc
    path.parent.mkdir(parents=True, exist_ok=True)
    _pd_str(None)                                   # load the list formatter once
    fmt_list = _PD_STR
    keys = [esc(c) + ":" for c in columns]
    pairs = list(zip(keys, columns))
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            parts = []
            for k, c in pairs:
                v = r.get(c)
                s = v if type(v) is str else (fmt_list(v) if isinstance(v, (list, tuple)) else str(v))
                parts.append(k + esc(s))
            f.write(("{" + ",".join(parts) + "}\n").replace("/", "\\/"))
            n += 1
    return n
