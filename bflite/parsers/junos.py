"""Juniper Junos parser: hierarchical or 'set' format -> flat set statements -> model.

Semantics follow Batfish's JuniperConfiguration (interfaces, BGP groups, router-id, VLANs).
"""
from __future__ import annotations

import fnmatch
import ipaddress
import re
from typing import Iterable, Optional

from ..model import DEFAULT_VRF, Address, BgpPeer, BgpProcess, Interface, Node, Vlan
from ..util import is_ipv4, parse_address, parse_asn, parse_vlan_list

JUNIPER = "JUNIPER"


# ---------------------------------------------------------------------------------------------
# Flattening
# ---------------------------------------------------------------------------------------------
_TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|[{};\[\]]|[^\s{};\[\]"]+')
# JuniperFlattener's lexer: a quoted string runs to the next '"', with no escapes, so the brace-format text
# "cust \"ACME\" link" becomes three tokens that Batfish joins with spaces (see _join_free_text).
_TOKEN_BRACE = re.compile(r'"[^"]*"|[{};\[\]]|[^\s{};\[\]"]+')
_FREE_TEXT = {"description"}


def _unquote(t: str) -> str:
    """Strip one pair of surrounding quotes, keeping backslashes (Batfish keeps them too)."""
    return t[1:-1] if len(t) >= 2 and t[0] == '"' and t[-1] == '"' else t


def _join_free_text(raw: list[str]) -> list[str]:
    """Unquote every token, except that a free-text value ('description ...') keeps Batfish's rest-of-line
    reading: the raw tokens joined with spaces, then one pair of outer quotes removed."""
    for k, t in enumerate(raw[:-1]):
        if t in _FREE_TEXT:
            return [_unquote(x) for x in raw[:k + 1]] + [_unquote(" ".join(raw[k + 1:]))]
    return [_unquote(x) for x in raw]


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    out = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            continue
        # trailing '## SECRET-DATA' style comments
        idx = line.find(" ## ")
        out.append(line[:idx] if idx >= 0 else line)
    return "\n".join(out)


def flatten(text: str, warnings: Optional[list[str]] = None) -> list[list[str]]:
    """Return a list of statements (token lists, without the leading 'set')."""
    text = _strip_comments(text)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    set_like = sum(1 for l in lines if l.startswith(("set ", "deactivate ", "delete ")))
    if lines and set_like >= max(1, len(lines) // 2):
        stmts: list[list[str]] = []
        deactivated: list[list[str]] = []
        for l in lines:
            raw = _TOKEN.findall(l)
            if not raw:
                continue
            toks = [raw[0]] + _join_free_text(raw[1:])
            if toks[0] == "set" and "[" in toks:
                # Batfish's flat-Juniper grammar has no '[ a b ]' lists (only JuniperFlattener expands
                # them, for brace-format files), so it reports the line as unrecognised and drops it.
                # 'show configuration | display set' never writes brackets.
                if warnings is not None:
                    warnings.append(f"This syntax is unrecognized: {l}")
                continue
            if toks[0] == "set":
                stmts.extend(_expand_brackets(toks[1:]))
            elif toks[0] in ("deactivate", "delete"):
                deactivated.append(toks[1:])
        return [s for s in stmts if not any(s[:len(d)] == d for d in deactivated)]

    tokens = _TOKEN_BRACE.findall(text)
    stmts = []
    stack: list[list[str]] = []
    cur: list[str] = []
    pending_inactive = False
    inactive_level: Optional[int] = None
    for t in tokens:
        if t == "{":
            stack.append(cur)
            if pending_inactive and inactive_level is None:
                inactive_level = len(stack)
            pending_inactive = False
            cur = []
        elif t == "}":
            if stack:
                stack.pop()
            if inactive_level is not None and len(stack) < inactive_level:
                inactive_level = None
            cur = []
        elif t == ";":
            if cur and not pending_inactive and inactive_level is None:
                head = [_unquote(w) for frame in stack for w in frame]
                stmts.extend(_expand_brackets(head + _join_free_text(cur)))
            pending_inactive = False
            cur = []
        elif t in ("inactive:", "replace:", "protect:") and not cur:
            if t == "inactive:":
                pending_inactive = True
        else:
            cur.append(t)
    return stmts


def _expand_brackets(toks: list[str]) -> list[list[str]]:
    if "[" not in toks:
        return [toks]
    a = toks.index("[")
    b = toks.index("]", a)
    out = []
    for item in toks[a + 1:b]:
        out.extend(_expand_brackets(toks[:a] + [item] + toks[b + 1:]))
    return out


def _glob(pattern: str) -> Optional[str]:
    if pattern.startswith("<") and pattern.endswith(">"):
        return pattern[1:-1]
    return None


def apply_groups(stmts: list[list[str]]) -> list[list[str]]:
    """Expand Junos 'groups' / 'apply-groups' (wildcards like <*> or <ge-*> supported)."""
    stmts = [s for s in stmts if s]
    groups: dict[str, list[list[str]]] = {}
    body: list[list[str]] = []
    applies: list[tuple[list[str], str]] = []
    excepts: set[tuple[tuple[str, ...], str]] = set()
    for s in stmts:
        if s[0] == "groups" and len(s) > 2:
            groups.setdefault(s[1], []).append(s[2:])
        elif s[-2:-1] == ["apply-groups"] or (len(s) >= 2 and s[-2] == "apply-groups"):
            applies.append((s[:-2], s[-1]))
        elif len(s) >= 2 and s[-2] == "apply-groups-except":
            excepts.add((tuple(s[:-2]), s[-1]))
        else:
            body.append(s)
    if not applies:
        return body
    existing = {tuple(s[:k]) for s in body for k in range(1, len(s) + 1)}
    out = list(body)
    seen = {tuple(s) for s in body}
    for level, gname in applies:
        for gs in groups.get(gname, []):
            if len(gs) < len(level):
                continue
            ok = all(_glob(p) is None and p == l or (_glob(p) is not None and fnmatch.fnmatch(l, _glob(p)))
                     for p, l in zip(gs, level))
            if not ok:
                continue
            tail = gs[len(level):]
            for concrete in _instantiate(level, tail, existing):
                # apply-groups-except at a sub-level
                if any(concrete[:len(lv)] == list(lv) and g == gname for lv, g in excepts):
                    continue
                if tuple(concrete) not in seen:
                    seen.add(tuple(concrete))
                    out.append(concrete)
    return out


def _instantiate(prefix: list[str], tail: list[str], existing: set) -> Iterable[list[str]]:
    if not tail:
        yield list(prefix)
        return
    head, rest = tail[0], tail[1:]
    pat = _glob(head)
    if pat is None:
        yield from _instantiate(prefix + [head], rest, existing)
        return
    # wildcard: match existing children at this level
    children = sorted({e[len(prefix)] for e in existing if len(e) > len(prefix) and list(e[:len(prefix)]) == prefix})
    for c in children:
        if fnmatch.fnmatch(c, pat):
            yield from _instantiate(prefix + [c], rest, existing)


_COMPONENT = r"\d+|\[\d+-\d+\]|\[[\d,]+\]"
_RANGE_MEMBER = re.compile(rf"^([a-z][a-z])-({_COMPONENT})/({_COMPONENT})/({_COMPONENT})$")
_RANGE_END = re.compile(r"^([a-z][a-z])-(\d+)/(\d+)/(\d+)$")


def _component(c: str) -> list[int]:
    if c.isdigit():
        return [int(c)]
    inner = c[1:-1]
    if "-" in inner:
        lo, hi = map(int, inner.split("-"))
        return list(range(lo, hi + 1))
    return [int(x) for x in inner.split(",")]


def _range_members(t: list[str]) -> list[str]:
    """InterfaceRangeMember ('ge-0/0/[1-4]', 'ge-0/0/[1,3]') and InterfaceRangeMemberRange ('A to B')."""
    if t[0] == "member" and len(t) > 1 and (m := _RANGE_MEMBER.match(t[1])):
        return [f"{m.group(1)}-{a}/{b}/{c}" for a in _component(m.group(2)) for b in _component(m.group(3))
                for c in _component(m.group(4))]
    if t[0] == "member-range" and len(t) >= 4 and t[2] == "to":
        a, b = _RANGE_END.match(t[1]), _RANGE_END.match(t[3])
        if a and b:
            lo, hi = [int(x) for x in a.groups()[1:]], [int(x) for x in b.groups()[1:]]
            return [f"{a.group(1)}-{f}/{p}/{q}" for f in range(lo[0], hi[0] + 1) for p in range(lo[1], hi[1] + 1)
                    for q in range(lo[2], hi[2] + 1)]
    return []


def _phys_field(t: list[str]) -> Optional[str]:
    """Physical settings an interface-range passes on (juniper Interface.inheritUnsetPhysicalFields)."""
    if t[0] in ("description", "mtu"):
        return t[0]
    if t[0] in ("ether-options", "gigether-options") and "802.3ad" in t:
        return "802.3ad"
    return None


def expand_interface_ranges(stmts: list[list[str]]) -> list[list[str]]:
    """LogicalSystem.expandInterfaceRange: every member (created if needed) takes the range's physical
    settings (description, MTU, 802.3ad) that it doesn't set itself."""
    ranges: dict[str, dict] = {}
    out: list[list[str]] = []
    for s in stmts:
        if s[:2] == ["interfaces", "interface-range"] and len(s) > 3:
            r = ranges.setdefault(s[2], {"members": [], "phys": []})
            t = s[3:]
            if t[0] in ("member", "member-range"):
                r["members"] += _range_members(t)
            elif t[0] != "unit":
                r["phys"].append(t)
            # 'interface-range R unit ...' lines are reported as unrecognized syntax by Batfish (probed
            # 2026-11-19 with description, ethernet-switching interface-mode/port-mode and vlan members), so
            # range members get no units from the range.
        else:
            out.append(s)
    if not ranges:
        return stmts
    have: dict[tuple, set] = {}
    for s in out:
        if s[0] == "interfaces" and len(s) > 2 and s[2] != "unit":
            f = _phys_field(s[2:])
            if f:
                have.setdefault((s[1],), set()).add(f)
    for name in sorted(ranges):
        r = ranges[name]
        for m in sorted(set(r["members"])):
            out.append(["interfaces", m, "__member-of-range__"])
            got = have.setdefault((m,), set())
            add = [t for t in r["phys"] if _phys_field(t) and _phys_field(t) not in got]
            out += [["interfaces", m] + t for t in add]
            got |= {_phys_field(t) for t in add}
    return out


# ---------------------------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------------------------
def _junos_kind(name: str) -> str:
    base = name.split(".")[0]
    if base.startswith("lo"):
        return "LOOPBACK"
    if base.startswith(("irb", "vlan")) and "." in name:
        return "VLAN"
    if base.startswith("ae"):
        return "AGGREGATE_CHILD" if "." in name else "AGGREGATED"
    if base.startswith(("st", "gr", "ip", "lt")) and "-" in base:
        return "TUNNEL"
    return "LOGICAL" if "." in name else "PHYSICAL"


def _junos_bandwidth(name: str) -> float:
    for p, bw in (("xe", 1e10), ("mge", 1e10), ("sxe", 1e10), ("ge", 1e9), ("fe", 1e8), ("irb", 1e9),
                  ("et", 1e11), ("em", 1e9)):
        if name.startswith(p):
            return bw
    return 1e12


class JunosParser:
    def __init__(self, text: str, filename: str = ""):
        self.text = text
        self.filename = filename

    def parse(self) -> Node:
        warnings: list[str] = []
        stmts = expand_interface_ranges(apply_groups(flatten(self.text, warnings)))
        host = None
        for s in stmts:
            if s[:2] == ["system", "host-name"] and len(s) > 2:
                host = s[2]
        hostname = (host or self.filename.rsplit("/", 1)[-1].rsplit(".", 1)[0]).lower()
        node = Node(hostname=hostname, vendor=JUNIPER, filename=self.filename)
        node.warnings.extend(warnings)
        self.node = node
        self.stmts = stmts
        self.policies = {s[2] for s in stmts if s[:2] == ["policy-options", "policy-statement"] and len(s) > 2}
        self.filters = {self._filter_name(s) for s in stmts if s and s[0] == "firewall"} - {None}
        self._vlans()
        self._interfaces()
        self._zones()
        self._vxlan()
        self._bgp()
        return node

    def _zones(self) -> None:
        """SRX 'security zones security-zone Z interfaces I': Zone_Name on interface I exactly as named."""
        for s in self.stmts:
            if s[:3] == ["security", "zones", "security-zone"] and len(s) > 5 and s[4] == "interfaces":
                itf = self.node.interfaces.get(s[5])
                if itf is not None:
                    itf.zone = s[3]

    def _vxlan(self) -> None:
        """JuniperConfiguration.convertVxlan: a VLAN's 'vxlan vni' becomes a Layer2Vni only when
        'switch-options vtep-source-interface' names an interface in the default instance that has a
        primary address. Without that, no VLAN gets a VNI."""
        src = next((s[2] for s in self.stmts if s[:2] == ["switch-options", "vtep-source-interface"]
                    and len(s) > 2), None)
        itf = self.node.interfaces.get(src) if src else None
        if itf is None or itf.vrf != DEFAULT_VRF or itf.primary is None:
            for v in self.node.vlans.values():
                v.vni = None

    @staticmethod
    def _filter_name(s: list[str]) -> Optional[str]:
        if "filter" in s:
            i = s.index("filter")
            return s[i + 1] if i + 1 < len(s) else None
        return None

    # -- VLANs
    def _vlans(self) -> None:
        self.vlan_by_name: dict[str, int] = {}
        self.l3_iface_vlan: dict[str, int] = {}
        self.vlan_vni: dict[int, int] = {}
        self.indirect_access: dict[str, int] = {}
        raw: dict[str, dict] = {}
        for s in self.stmts:
            if s[0] == "vlans" and len(s) > 2:
                d = raw.setdefault(s[1], {})
                if s[2] == "vlan-id" and len(s) > 3 and s[3].isdigit():
                    d["id"] = int(s[3])
                elif s[2] == "l3-interface" and len(s) > 3:
                    d["l3"] = s[3]
                elif s[2:4] == ["vxlan", "vni"] and len(s) > 4:
                    d["vni"] = int(s[4])
                elif s[2] == "interface" and len(s) > 3:
                    d.setdefault("ifaces", []).append(s[3] if "." in s[3] else s[3] + ".0")
        for name, d in raw.items():
            vid = d.get("id")
            if vid is None:
                continue
            self.vlan_by_name[name] = vid
            v = self.node.vlans.setdefault(vid, Vlan(vid, name=name))
            if "vni" in d:
                v.vni = d["vni"]
            if "l3" in d and d["l3"].startswith("irb"):   # old EX 'vlan.N' l3-interfaces get no VLAN in Batfish
                self.l3_iface_vlan[d["l3"]] = vid
            for itf in d.get("ifaces", []):
                self.indirect_access[itf] = vid

    def _vlan_members(self, members: list[str]) -> set[int]:
        out: set[int] = set()
        for m in members:
            if m == "all":
                out.update(range(1, 4095))
            elif re.match(r"^[\d\-,]+$", m):
                out |= parse_vlan_list(m)
            elif m in self.vlan_by_name:
                out.add(self.vlan_by_name[m])
        return out

    # -- interfaces
    def _interfaces(self) -> None:
        phys: dict[str, dict] = {}
        units: dict[str, dict] = {}
        for s in self.stmts:
            if s[0] != "interfaces" or len(s) < 3:
                continue
            ifname = s[1]
            if ifname in ("interface-range", "traceoptions"):
                continue
            p = phys.setdefault(ifname, {})
            rest = s[2:]
            if rest[0] == "unit" and len(rest) > 1:
                uname = f"{ifname}.{rest[1]}"
                u = units.setdefault(uname, {"parent": ifname, "addrs": [], "primary": None, "members": []})
                r = rest[2:]
                if not r:
                    continue
                if r[0] == "description":
                    u["description"] = " ".join(r[1:])
                elif r[0] == "disable":
                    u["disable"] = True
                elif r[0] == "vlan-id" and len(r) > 1:
                    u["vlan_id"] = int(r[1])
                elif r[:2] == ["family", "inet"] and len(r) > 2:
                    f = r[2:]
                    if f[0] == "address" and "vrrp-group" in f and f.index("vrrp-group") + 1 < len(f):
                        g = f[f.index("vrrp-group") + 1]
                        if g.isdigit() and int(g) not in u.setdefault("vrrp", []):
                            u["vrrp"].append(int(g))
                    if f[0] == "address" and len(f) > 1:
                        a = parse_address([f[1]])
                        if a and a not in u["addrs"]:
                            u["addrs"].append(a)
                        if a and len(f) > 2 and f[2] == "primary":
                            u["primary"] = a
                    elif f[0] == "filter" and len(f) > 2:
                        u["in" if f[1] == "input" else "out"] = f[2]
                    elif f[0] == "mtu" and len(f) > 1:
                        u["mtu"] = int(f[1])
                    else:
                        u.setdefault("inet", True)
                elif r[:2] == ["family", "ethernet-switching"] or r[:2] == ["family", "bridge"]:
                    u["es"] = True
                    f = r[2:]
                    if f and f[0] in ("interface-mode", "port-mode") and len(f) > 1:
                        u["mode"] = f[1].upper()
                    elif f[:2] == ["vlan", "members"] and len(f) > 2:
                        u["members"].append(f[2])
                    elif f and f[0] == "native-vlan-id" and len(f) > 1:
                        u["native"] = int(f[1])
            else:
                if rest[0] == "description":
                    p["description"] = " ".join(rest[1:])
                elif rest[0] == "disable":
                    p["disable"] = True
                elif rest[0] == "mtu" and len(rest) > 1:
                    p["mtu"] = int(rest[1])
                elif rest[0] in ("ether-options", "gigether-options") and len(rest) > 2 and rest[1] == "802.3ad":
                    p["lag"] = rest[2] if rest[2] not in ("lacp", "backup", "primary") else rest[-1]
                elif rest[0] == "native-vlan-id" and len(rest) > 1:
                    p["native"] = int(rest[1])
                elif rest[0] == "speed" and len(rest) > 1:
                    p["speed"] = rest[1]
                elif rest[0] == "description":
                    p["description"] = " ".join(rest[1:])

        # routing instances
        self.ri_of: dict[str, str] = {}
        self.ri_type: dict[str, str] = {}
        for s in self.stmts:
            if s[0] == "routing-instances" and len(s) > 3:
                if s[2] == "interface":
                    nm = s[3] if "." in s[3] else s[3] + ".0"
                    self.ri_of[nm] = s[1]
                elif s[2] == "instance-type":
                    self.ri_type[s[1]] = s[3]
        for ri, t in self.ri_type.items():
            if t in ("vrf", "virtual-router", "forwarding"):
                self.node.vrfs.add(ri)
        for ri in {v for v in self.ri_of.values()}:
            self.node.vrfs.add(ri)

        node = self.node
        for name, p in phys.items():
            if name == "irb":   # JuniperConfiguration.resolveInterfacePointers: 'irb' is only a namespace
                continue
            itf = node.iface(name)
            itf.kind = _junos_kind(name)
            itf.description = p.get("description")
            itf.admin_up = not p.get("disable", False)
            itf.bandwidth = _junos_bandwidth(name)
            itf.speed = None
            itf.mtu = p.get("mtu", 1500)
            itf.proxy_arp = False
            itf.native_vlan = p.get("native")
            itf.switchport = False
            itf.switchport_mode = "NONE"
            if p.get("lag"):
                itf.channel_group = p["lag"]
            itf.vrf = DEFAULT_VRF
        for uname, u in units.items():
            parent = phys.get(u["parent"], {})
            itf = node.iface(uname)
            itf.parent = u["parent"]
            itf.kind = _junos_kind(uname)
            # juniper Interface.inheritUnsetFields: a unit takes the physical description and MTU
            # when it has none of its own ('irb' has no inheritable settings)
            itf.description = u.get("description", parent.get("description"))
            itf.admin_up = not u.get("disable", False)
            itf.bandwidth = _junos_bandwidth(uname)
            itf.mtu = u.get("mtu", parent.get("mtu", 1500))
            itf.proxy_arp = False
            itf.vrf = self.ri_of.get(uname, DEFAULT_VRF)
            prim = u["primary"]
            if prim is None and u["addrs"]:
                prim = u["addrs"][0]   # Batfish: first configured address unless one is flagged 'primary'
            if prim:
                itf.addresses = [prim] + [a for a in u["addrs"] if a != prim]
            if u.get("in") in self.filters:
                itf.in_filter = u["in"]
            if u.get("out") in self.filters:
                itf.out_filter = u["out"]
            itf.encapsulation_vlan = u.get("vlan_id")
            itf.vrrp_groups = sorted(u.get("vrrp", []))
            if uname in self.l3_iface_vlan:
                itf.vlan_id = self.l3_iface_vlan[uname]
            elif itf.kind == "VLAN":
                itf.vlan_id = None
                if uname.startswith("irb."):   # JuniperConfiguration: IRB unit with no VLAN -> INCOMPLETE
                    itf.inactive_reason_pre = "Incomplete configuration"
            itf.switchport, itf.switchport_mode = False, "NONE"
            if uname in self.indirect_access:
                itf.switchport, itf.switchport_mode = True, "ACCESS"
                itf.access_vlan = self.indirect_access[uname]
            elif u.get("es"):
                mode = u.get("mode")
                members = self._vlan_members(u["members"])
                if mode in (None, "ACCESS"):
                    av = min(members) if members else None
                    itf.access_vlan = av
                    if av is not None:
                        itf.switchport, itf.switchport_mode = True, "ACCESS"
                if mode == "TRUNK":
                    itf.allowed_vlans = members
                    itf.native_vlan = u.get("native", parent.get("native"))
                    itf.switchport, itf.switchport_mode = True, "TRUNK"
                    itf.trunk_encapsulation = "DOT1Q"

    # -- BGP
    def _bgp(self) -> None:
        # router-id / AS per routing instance
        ri_rid: dict[str, str] = {}
        ri_as: dict[str, int] = {}
        ri_confed: dict[str, tuple] = {}
        bgp_stmts: dict[str, list[list[str]]] = {}
        for s in self.stmts:
            if s[:2] == ["routing-options", "router-id"] and len(s) > 2:
                ri_rid[DEFAULT_VRF] = s[2]
            elif s[:2] == ["routing-options", "autonomous-system"] and len(s) > 2:
                ri_as[DEFAULT_VRF] = parse_asn(s[2])
            elif s[:3] == ["protocols", "bgp"] or (s[:2] == ["protocols", "bgp"]):
                bgp_stmts.setdefault(DEFAULT_VRF, []).append(s[2:])
            elif s[0] == "routing-instances" and len(s) > 3:
                ri = s[1]
                if s[2:4] == ["routing-options", "router-id"] and len(s) > 4:
                    ri_rid[ri] = s[4]
                elif s[2:4] == ["routing-options", "autonomous-system"] and len(s) > 4:
                    ri_as[ri] = parse_asn(s[4])
                elif s[2:4] == ["protocols", "bgp"]:
                    bgp_stmts.setdefault(ri, []).append(s[4:])
            if s[:3] == ["routing-options", "confederation"] and len(s) > 3:
                cid, members = ri_confed.get(DEFAULT_VRF, (None, []))
                if s[3] == "members":
                    members = members + s[4:]
                else:
                    cid = parse_asn(s[3])
                    if "members" in s:
                        members = members + s[s.index("members") + 1:]
                ri_confed[DEFAULT_VRF] = (cid, members)

        # default instance router-id falls back to lo0.0 primary
        if DEFAULT_VRF not in ri_rid:
            lo = self.node.interfaces.get("lo0.0")
            if lo and lo.primary and lo.vrf == DEFAULT_VRF:
                ri_rid[DEFAULT_VRF] = str(lo.primary.ip)

        for ri, lines in bgp_stmts.items():
            self._bgp_instance(ri, lines, ri_rid.get(ri), ri_as.get(ri, ri_as.get(DEFAULT_VRF)),
                               ri_confed.get(ri))

    def _bgp_instance(self, ri: str, lines: list[list[str]], rid: Optional[str], ri_asn: Optional[int],
                      confed: Optional[tuple]) -> None:
        top: dict = {"import": [], "export": []}
        groups: dict[str, dict] = {}
        neighbors: dict[tuple[str, str], dict] = {}
        allows: list[tuple[str, str]] = []
        proc_flags = {"external_router_id": False, "cluster_len": False}

        def apply(d: dict, toks: list[str]) -> None:
            if not toks:
                return
            k = toks[0]
            if k == "type" and len(toks) > 1:
                d["type"] = toks[1]
            elif k == "peer-as" and len(toks) > 1:
                d["peer_as"] = parse_asn(toks[1])
            elif k == "local-as" and len(toks) > 1:
                d["local_as"] = parse_asn(toks[1])
            elif k == "local-address" and len(toks) > 1:
                d["local_address"] = toks[1]
            elif k == "description":
                d["description"] = " ".join(toks[1:])
            elif k == "import" and len(toks) > 1:
                d["import"] = list(dict.fromkeys(d.get("import", []) + toks[1:]))   # a policy is listed once
            elif k == "export" and len(toks) > 1:
                d["export"] = list(dict.fromkeys(d.get("export", []) + toks[1:]))
            elif k == "multipath":
                d["multipath"] = True
                if len(toks) > 1 and toks[1] == "multiple-as":
                    d["multiple_as"] = True
            elif k == "cluster" and len(toks) > 1:
                d["cluster"] = toks[1]
            elif k == "multihop":
                d["multihop"] = True
            elif k == "family" and len(toks) > 1 and toks[1] == "evpn":
                d["evpn"] = True
            elif k == "disable":
                d["disable"] = True
            elif k == "path-selection" and len(toks) > 1:
                if toks[1] == "external-router-id":
                    proc_flags["external_router_id"] = True
                elif toks[1] == "cisco-non-deterministic":
                    pass

        for t in lines:
            if not t:
                continue
            if t[0] == "group" and len(t) > 1:
                g = groups.setdefault(t[1], {})
                r = t[2:]
                if r[:1] == ["neighbor"] and len(r) > 1:
                    n = neighbors.setdefault((t[1], r[1]), {})
                    apply(n, r[2:])
                elif r[:1] == ["allow"] and len(r) > 1:
                    allows.append((t[1], r[1]))
                else:
                    apply(g, r)
            elif t[0] == "neighbor" and len(t) > 1:
                n = neighbors.setdefault(("", t[1]), {})
                apply(n, t[2:])
            else:
                apply(top, t)
        if top.get("disable"):
            return

        bp = BgpProcess(vrf=ri)
        bp.router_id = rid  # None -> inferred in finalize (Junos rule)
        if proc_flags["external_router_id"]:
            bp.tie_breaker = "ROUTER_ID"
        if confed and confed[0] is not None and confed[1]:
            bp.confederation_id = confed[0]
            bp.confederation_members = ",".join(str(parse_asn(m)) for m in confed[1])
        local_as_top = top.get("local_as", ri_asn)
        bp.local_as = local_as_top

        mp = {"ibgp": None, "ebgp": None, "mas": None}

        def merged(gname: str, n: dict) -> dict:
            out: dict = {}
            for layer in (top, groups.get(gname, {}), n):
                for k, v in layer.items():
                    if k in ("import", "export"):
                        out[k] = v      # Junos: lower level replaces the list
                    else:
                        out[k] = v
            return out

        entries = [(g, ip, n, False) for (g, ip), n in neighbors.items()]
        entries += [(g, pfx, {}, True) for g, pfx in allows]
        for gname, addr, n, dynamic in entries:
            s = merged(gname, n)
            if s.get("disable"):
                continue
            if ":" in addr:
                continue
            local_as = s.get("local_as", local_as_top)
            gtype = s.get("type")
            peer_as = s.get("peer_as")
            if gtype == "external":
                ibgp = False
            elif gtype == "internal":
                ibgp = True
            else:
                ibgp = peer_as is not None and peer_as == local_as
            remote_as = local_as if ibgp else peer_as
            p = BgpPeer(key=addr, vrf=ri)
            if dynamic:
                p.remote_prefix = str(ipaddress.IPv4Network(addr, strict=False))
                p.key = p.remote_prefix
                p.remote_as = str(remote_as) if remote_as is not None else "0-4294967295"
            else:
                p.remote_ip = addr.split("/")[0]
                p.key = p.remote_ip
                p.remote_as = str(remote_as) if remote_as is not None else ""
            p.local_as = local_as
            p.local_ip = s.get("local_address")
            p.description = s.get("description")
            p.peer_group = gname or None
            p.ebgp_multihop = bool(s.get("multihop"))
            p.send_community = True
            p.families = {"IPV4_UNICAST"} | ({"EVPN"} if s.get("evpn") else set())
            p.import_policy = [x for x in s.get("import", []) if x in self.policies]
            p.export_policy = [x for x in s.get("export", []) if x in self.policies]
            if s.get("cluster"):
                p.rr_client = True
                p.cluster_id = s["cluster"]
            p.confederation = bp.confederation_id
            p.junos_explicit_local_ip = True  # type: ignore[attr-defined]
            bp.peers[p.key] = p
            # multipath bookkeeping (Batfish: conflicting groups force False)
            m = bool(s.get("multipath"))
            if ibgp:
                mp["ibgp"] = m if mp["ibgp"] in (None, m) else False
            else:
                mp["ebgp"] = m if mp["ebgp"] in (None, m) else False
                mas = bool(s.get("multiple_as"))
                mp["mas"] = mas if mp["mas"] in (None, mas) else False
        bp.multipath_ibgp = bool(mp["ibgp"])
        bp.multipath_ebgp = bool(mp["ebgp"])
        bp.multipath_match_mode = "PATH_LENGTH" if mp["mas"] else "FIRST_AS"
        self.node.bgp[ri] = bp
