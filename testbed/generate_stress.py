"""Generate a stress-test bed of 1,000 configs: Cisco IOS / IOS-XE, NX-OS and Junos of many ages and roles.

    python testbed/generate_stress.py [--count 1000] [--seed 7]

Writes testbed/stress/snapshot/configs/ and testbed/stress/manifest.json, which lists every file with its
role, model, OS version, format, the edge cases it carries and any deliberate BGP faults. The output is
deterministic for a given seed, and is gitignored: rerun the script to recreate it.

Ages: IOS 12.2/12.4, 15.x and IOS-XE 16/17; NX-OS 6.2, 7.x, 9.3 and 10.x on N7K, N5K, N3K and N9K;
Junos 12.3 (old non-ELS EX), 15.1, 18.4, 19.4, 20.4, 21.4 and 23.2, in brace and 'display set' format.
Each age gets its own syntax (ip vrf vs vrf definition, port-mode vs interface-mode, BGP at the top level vs
per address family, switchname, kickstart images, me0 vs em0 ...).

Roles: IOS-XE/IOS campuses, NX-OS data centres (VXLAN EVPN with vPC, and classic N7K/N5K), Junos campuses,
branches (sub-interfaces, GRE tunnels, dynamic BGP), MX PEs with VRFs to CE routers, and internet edges
with peers outside the snapshot, all hanging off a mixed IOS-XE / MX backbone (AS 64512).

Edge cases, scattered at random and recorded per file: CRLF line endings, RANCID headers, banners holding
config-like lines, quotes, brackets, commas, unicode and Latin-1 bytes in descriptions, very long
descriptions, crypto certificates, wrapped 'allowed vlan add' lists, secondary addresses, undefined ACLs
and route-maps, IPv6 BGP peers, 4-byte and asdot ASNs, Junos interface-range, inactive:/deactivate, wildcard
groups, stanzas in an unusual order, missing hostnames, upper-case hostnames, and a few junk files (empty,
truncated, a README, a duplicate hostname).
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import random
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE / "stress"

# model: (family, [(version, weight)], port layout [(prefix, first, last)], kind)
PLATFORMS = {
    "C2960": ("ios", [("12.2(55)SE12", 3), ("15.0(2)SE11", 2)], [("FastEthernet0/", 1, 24), ("GigabitEthernet0/", 1, 2)], "switch"),
    "C3750": ("ios", [("12.2(55)SE12", 3), ("15.0(2)SE11", 2)], [("FastEthernet1/0/", 1, 48), ("GigabitEthernet1/0/", 1, 4)], "switch"),
    "C3850": ("ios", [("16.9.8", 1), ("16.12.10", 1)], [("GigabitEthernet1/0/", 1, 48), ("TenGigabitEthernet1/1/", 1, 4)], "switch"),
    "C9300": ("ios", [("17.6.5", 1), ("17.9.4a", 2), ("17.12.3", 1)], [("GigabitEthernet1/0/", 1, 48), ("TenGigabitEthernet1/1/", 1, 8)], "switch"),
    "C9500": ("ios", [("17.9.4a", 1), ("17.12.3", 1)], [("TwentyFiveGigE1/0/", 1, 24), ("HundredGigE1/0/", 25, 28)], "switch"),
    "C6509": ("ios", [("12.2(33)SXJ10", 1), ("15.1(2)SY16", 1)],
              [(f"GigabitEthernet{m}/", 1, 48) for m in (1, 2, 3, 4)] + [("TenGigabitEthernet5/", 1, 8)], "switch"),
    "ISR2911": ("ios", [("12.4(24)T8", 1), ("15.4(3)M10", 2), ("15.7(3)M9", 2)], [("GigabitEthernet0/", 0, 2)], "router"),
    "ISR4451": ("ios", [("16.9.8", 1), ("17.6.5", 2)], [("GigabitEthernet0/0/", 0, 3)], "router"),
    "ASR1001X": ("ios", [("16.12.10", 1), ("17.9.4a", 2)], [("GigabitEthernet0/0/", 0, 5), ("TenGigabitEthernet0/1/", 0, 1)], "router"),
    "N7K": ("nxos", [("6.2(24)", 1), ("8.4(7)", 1)], [(f"Ethernet{m}/", 1, 48) for m in (1, 2, 3)], "switch"),
    "N5K": ("nxos", [("7.3(8)N1(1)", 1)], [("Ethernet1/", 1, 32), ("Ethernet2/", 1, 16)], "switch"),
    "N3K": ("nxos", [("7.0(3)I7(9)", 1), ("9.3(10)", 1)], [("Ethernet1/", 1, 52)], "switch"),
    "N9K": ("nxos", [("7.0(3)I7(10)", 1), ("9.3(12)", 3), ("10.2(6)", 1), ("10.3(4a)", 2)], [("Ethernet1/", 1, 54)], "switch"),
    "EX4200": ("junos", [("12.3R12.4", 1)], [("ge-0/0/", 0, 47), ("xe-0/1/", 0, 3)], "switch"),
    "EX4300": ("junos", [("15.1R7.9", 1), ("18.4R3-S9", 2), ("21.4R3-S5", 2)], [("ge-0/0/", 0, 47), ("xe-0/2/", 0, 3)], "switch"),
    "EX9208": ("junos", [("18.4R3-S9", 1), ("21.4R3-S5", 1)], [("xe-1/0/", 0, 39), ("et-2/0/", 0, 5)], "switch"),
    "QFX5120": ("junos", [("21.4R3-S5", 2), ("23.2R2", 1)], [("xe-0/0/", 0, 47), ("et-0/0/", 48, 55)], "switch"),
    "MX204": ("junos", [("21.4R3-S5", 1), ("23.2R2", 1)], [("xe-0/1/", 0, 7), ("et-0/0/", 0, 3)], "router"),
    "MX960": ("junos", [("18.4R3-S9", 1), ("20.4R3-S8", 1)], [(f"xe-{m}/0/", 0, 9) for m in (0, 1, 2)], "router"),
    "SRX1500": ("junos", [("19.4R3-S10", 1), ("21.4R3-S5", 1)], [("ge-0/0/", 0, 11), ("xe-0/0/", 16, 19)], "router"),
}

QUIRKS = {   # quirk: probability per device (families it applies to)
    "crlf": (0.04, "any"), "rancid": (0.10, "any"), "banner": (0.15, "any"), "unicode_desc": (0.05, "any"),
    "quote_desc": (0.06, "any"), "long_desc": (0.03, "any"), "trailing_ws": (0.04, "any"),
    "uppercase_host": (0.03, "any"), "secondary_ip": (0.06, "ios"), "undefined_acl": (0.05, "ios"),
    "undefined_policy": (0.05, "any"), "v6_peer": (0.05, "any"), "cert": (0.08, "ios"),
    "odd_order": (0.03, "ios"), "no_hostname": (0.01, "ios"), "latin1": (0.01, "ios"),
    "interface_range": (0.15, "junos-ex"), "inactive": (0.08, "junos"), "apply_groups": (0.25, "junos"),
    "no_local_address": (0.30, "junos"), "no_default_ipv4": (0.15, "ios-new"), "portfast_default": (0.4, "ios-switch"),
}


# ---------------------------------------------------------------------------------------------- model
@dataclass
class If:
    name: str
    mode: str = "unused"        # unused routed access trunk svi loopback mgmt tunnel lag-member
    ips: list = field(default_factory=list)
    vrf: str | None = None
    desc: str | None = None
    shut: bool = False
    vlan: int | None = None     # access VLAN, SVI VLAN or sub-interface tag
    vlans: list | None = None   # trunk allowed VLANs (None = all)
    native: int | None = None
    lag: str | None = None      # parent aggregate of a member
    mtu: int | None = None
    speed: int | None = None
    fhrp: tuple | None = None   # (kind, group, vip)
    helpers: list = field(default_factory=list)
    acl_in: str | None = None
    unnumbered: str | None = None
    tunnel_src: str | None = None
    vpc: str | None = None


@dataclass
class Dev:
    name: str
    model: str
    version: str
    site: int
    role: str
    asn: int | None = None
    lo: str | None = None
    fmt: str = "native"
    ifs: dict = field(default_factory=dict)
    vlans: dict = field(default_factory=dict)       # id -> (name, vni)
    vrfs: dict = field(default_factory=dict)        # name -> {"rd": str, "vni": int | None}
    nbrs: list = field(default_factory=list)
    listen: list = field(default_factory=list)      # (prefix, group, remote_as)
    statics: list = field(default_factory=list)     # (prefix, next hop, vrf)
    ospf: bool = False
    quirks: set = field(default_factory=set)
    faults: list = field(default_factory=list)
    acls: set = field(default_factory=set)
    policies: set = field(default_factory=set)
    rr: bool = False

    def __post_init__(self):
        self.family = PLATFORMS[self.model][0]
        self.kind = PLATFORMS[self.model][3]
        self.ports = [f"{p}{n}" for p, a, b in PLATFORMS[self.model][2] for n in range(a, b + 1)]
        self.free = list(self.ports)
        for p in self.ports:
            self.ifs[p] = If(p)

    @property
    def old(self) -> bool:
        return self.version.startswith(("12.", "6.")) or self.version.startswith("12.3R")

    @property
    def els(self) -> bool:          # Junos Enhanced Layer 2 Software (EX4300+, QFX, EX9200)
        return self.model != "EX4200"

    def take(self) -> str:
        return self.free.pop(0)

    def take_up(self) -> str:
        return self.free.pop()


class Pool:
    def __init__(self, cidr: str, plen: int):
        self.it = ipaddress.IPv4Network(cidr).subnets(new_prefix=plen)

    def next(self):
        return next(self.it)


def pick(rng: random.Random, items):
    vals, weights = zip(*items)
    return rng.choices(vals, weights)[0]


def new_dev(rng, name, model, site, role, **kw) -> Dev:
    d = Dev(name, model, pick(rng, PLATFORMS[model][1]), site, role, **kw)
    fam = d.family
    for q, (p, scope) in QUIRKS.items():
        ok = (scope == "any" or scope == fam or (scope == "junos-ex" and model.startswith("EX"))
              or (scope == "ios-new" and fam == "ios" and not d.version.startswith("12."))
              or (scope == "ios-switch" and fam == "ios" and d.kind == "switch"))
        if ok and rng.random() < p:
            d.quirks.add(q)
    if fam == "junos":
        d.fmt = "set" if rng.random() < 0.4 else "brace"
    return d


# ---------------------------------------------------------------------------------------------- helpers
def names(d: Dev):
    f = d.family
    return {
        "lo": {"ios": "Loopback0", "nxos": "loopback0", "junos": "lo0.0"}[f],
        "svi": (lambda v: {"ios": f"Vlan{v}", "nxos": f"Vlan{v}"}.get(f) or (f"irb.{v}" if d.els else f"vlan.{v}")),
        "lag": (lambda n: {"ios": f"Port-channel{n}", "nxos": f"port-channel{n}", "junos": f"ae{n}"}[f]),
    }


DESC_ODD = ['cust "ACME" link', "it's [primary], A/B", "uplink, 10G / LACP #1", "rack 4 shelf 2 & 3"]
DESC_UNICODE = ["Büro Zürich", "café uplink", "数据中心 uplink", "link ✓ ok"]


def desc(d: Dev, rng, text: str) -> str:
    if "quote_desc" in d.quirks and rng.random() < 0.3:
        return rng.choice(DESC_ODD)
    if "unicode_desc" in d.quirks and rng.random() < 0.3:
        return rng.choice(DESC_UNICODE)
    if "long_desc" in d.quirks and rng.random() < 0.2:
        return (text + " ") * 20
    return text


def loopback(d: Dev, ip: str):
    d.lo = ip
    n = names(d)["lo"]
    d.ifs[n] = If(n, "loopback", [f"{ip}/32"])


def mgmt(d: Dev, ip: str):
    f = d.family
    if f == "nxos":
        d.ifs["mgmt0"] = If("mgmt0", "mgmt", [f"{ip}/24"], vrf="management")
    elif f == "junos":
        n = "fxp0.0" if d.model.startswith(("MX", "SRX")) else ("me0.0" if d.model == "EX4200" else "em0.0")
        d.ifs[n] = If(n, "mgmt", [f"{ip}/24"])
    elif d.model in ("C9300", "C9500", "C3850", "ISR4451", "ASR1001X"):
        n = {"ISR4451": "GigabitEthernet0", "ASR1001X": "GigabitEthernet0"}.get(d.model, "GigabitEthernet0/0")
        d.vrfs.setdefault("Mgmt-vrf" if d.kind == "switch" else "Mgmt-intf", {"rd": None, "vni": None})
        d.ifs[n] = If(n, "mgmt", [f"{ip}/24"], vrf="Mgmt-vrf" if d.kind == "switch" else "Mgmt-intf")


def routed(d: Dev, port: str, ip: str, text: str, rng, vrf=None, tag=None) -> str:
    name = port
    if tag is not None:
        name = f"{port}.{tag}"
        d.ifs[port].mode = "routed-parent"
    i = d.ifs.setdefault(name, If(name))
    i.mode, i.ips, i.vrf, i.vlan = "routed", [ip], vrf, tag
    i.desc = desc(d, rng, text)
    if "secondary_ip" in d.quirks and rng.random() < 0.2:
        i.ips.append(f"10.{200 + d.site % 50}.{rng.randrange(256)}.1/30")
    return name


def nbr(ip, asn, text, **kw) -> dict:
    return {"ip": ip, "asn": asn, "desc": text, **kw}


FAULTS = ["wrong_as", "one_sided", "typo_ip", "shut_if", "peer_shut", "no_remote_as"]


def session(a: Dev, b: Dev, ip_a: str, ip_b: str, rng, a_kw=None, b_kw=None, a_if=None, b_if=None,
            fault_rate=0.04):
    na = nbr(ip_b, b.asn, b.name, **(a_kw or {}))
    nb = nbr(ip_a, a.asn, a.name, **(b_kw or {}))
    if a.family == "junos" and "no_local_address" not in a.quirks and "local" not in na:
        na["local"] = ip_a
    if b.family == "junos" and "no_local_address" not in b.quirks and "local" not in nb:
        nb["local"] = ip_b
    add_b = True
    if rng.random() < fault_rate:
        f = rng.choice(FAULTS)
        if f == "wrong_as":
            na["asn"] = (b.asn or 0) + 1
        elif f == "one_sided":
            add_b = False
        elif f == "typo_ip":
            na["ip"] = str(ipaddress.IPv4Address(ip_b) + 2)
        elif f == "shut_if" and b_if:
            b.ifs[b_if].shut = True
        elif f == "peer_shut":
            na["shut"] = True
        elif f == "no_remote_as":
            na["asn"] = None
        a.faults.append(f"{f}: {a.name} -> {b.name} ({ip_b})")
    a.nbrs.append(na)
    if add_b:
        b.nbrs.append(nb)


def p2p_bgp(a: Dev, b: Dev, pool: Pool, rng, *, vrf_a=None, vrf_b=None, tag=None, a_port=None, b_port=None, **kw):
    net = pool.next()
    ip_a, ip_b = str(net[0]), str(net[1])
    ia = routed(a, a_port or a.take_up(), f"{ip_a}/31", f"to {b.name}", rng, vrf_a, tag)
    ib = routed(b, b_port or b.take_up(), f"{ip_b}/31", f"to {a.name}", rng, vrf_b, tag)
    session(a, b, ip_a, ip_b, rng, {"vrf": vrf_a, **kw.get("a_kw", {})}, {"vrf": vrf_b, **kw.get("b_kw", {})},
            ia, ib)
    return ia, ib


def ibgp_pair(a: Dev, b: Dev, pool: Pool, rng, statics=True):
    """Two routers in one AS: a routed link, iBGP over loopbacks, reachability by static or OSPF."""
    net = pool.next()
    ip_a, ip_b = str(net[0]), str(net[1])
    routed(a, a.take_up(), f"{ip_a}/31", f"to {b.name}", rng)
    routed(b, b.take_up(), f"{ip_b}/31", f"to {a.name}", rng)
    if statics:
        a.statics.append((f"{b.lo}/32", ip_b, None))
        b.statics.append((f"{a.lo}/32", ip_a, None))
    else:
        a.ospf = b.ospf = True
    upd = rng.random() > 0.04
    if not upd:
        a.faults.append(f"no_update_source: {a.name} -> {b.name}")
    session(a, b, a.lo, b.lo, rng, {"upd": upd, "group": "IBGP", "local": a.lo},
            {"upd": True, "group": "IBGP", "local": b.lo}, fault_rate=0.02)


def trunk(d: Dev, port: str, vlans, text, rng, native=None, lag=None) -> str:
    i = d.ifs[port]
    i.mode, i.vlans, i.native, i.lag, i.desc = "trunk", vlans, native, lag, desc(d, rng, text)
    return port


def make_lag(d: Dev, n: int, members: list[str], vlans, text, rng, vpc=None) -> str:
    name = names(d)["lag"](n)
    d.ifs[name] = If(name, "trunk", vlans=vlans, desc=desc(d, rng, text), vpc=vpc)
    for m in members:
        d.ifs[m].mode, d.ifs[m].lag, d.ifs[m].desc = "lag-member", name, desc(d, rng, text)
    return name


def svi(d: Dev, vlan: int, ip: str, rng, vrf=None, fhrp=None, helpers=()):
    n = names(d)["svi"](vlan)
    d.ifs[n] = If(n, "svi", [ip], vrf=vrf, vlan=vlan, fhrp=fhrp, helpers=list(helpers),
                  desc=desc(d, rng, f"SVI {vlan}"))


def access_ports(d: Dev, vlans: list[int], rng, count: int):
    for _ in range(min(count, len(d.free) - 4)):
        p = d.take()
        i = d.ifs[p]
        i.mode, i.vlan = "access", rng.choice(vlans)
        i.desc = desc(d, rng, f"user port {p.split('/')[-1]}") if rng.random() < 0.6 else None
        if rng.random() < 0.05:
            i.shut = True


# ---------------------------------------------------------------------------------------------- sites
class Site:
    def __init__(self, idx: int, rng: random.Random):
        self.idx, self.rng = idx, rng
        self.p2p = Pool(f"10.{idx}.250.0/24", 31)
        self.lo = (f"172.16.{idx}.{n}" for n in range(1, 255))
        self.mgmt = (f"192.168.{idx}.{n}" for n in range(10, 255))
        self.asn = {0: 65000 + idx, 1: 4200000000 + idx, 2: 65000 + idx}[rng.choice([0, 0, 1])]
        self.devs: list[Dev] = []

    def add(self, d: Dev) -> Dev:
        self.devs.append(d)
        mgmt(d, next(self.mgmt))
        return d


def campus_ios(s: Site, n: int, bb: list[Dev], bbpool: Pool):
    r = s.rng
    core_model = pick(r, [("C6509", 1), ("C9500", 2), ("C3850", 1), ("C9300", 1)])
    cores = [s.add(new_dev(r, f"s{s.idx:03d}-core{k}", core_model, s.idx, "campus core", asn=s.asn)) for k in (1, 2)]
    access_vlans = [100 + k for k in range(n)]
    all_vlans = [10, 20] + access_vlans
    for c in cores:
        loopback(c, next(s.lo))
        c.vlans = {v: (f"VLAN{v}", None) for v in all_vlans}
        c.vlans[10], c.vlans[20] = ("MGMT", None), ("VOICE", None)
    ibgp_pair(cores[0], cores[1], s.p2p, r, statics=r.random() < 0.5)
    for k, c in enumerate(cores):
        p2p_bgp(c, bb[(s.idx + k) % len(bb)], bbpool, r)
        for v in all_vlans:
            fh = ("hsrp", v % 256, f"10.{s.idx}.{v % 250}.1") if r.random() < 0.85 else ("vrrp", v % 256, f"10.{s.idx}.{v % 250}.1")
            svi(c, v, f"10.{s.idx}.{v % 250}.{2 + k}/24", r, fhrp=fh,
                helpers=["10.255.6.1"] if v >= 100 and r.random() < 0.5 else ())
    for k in range(n):
        model = pick(r, [("C2960", 2), ("C3750", 3), ("C3850", 2), ("C9300", 4)])
        a = s.add(new_dev(r, f"s{s.idx:03d}-acc{k + 1:02d}", model, s.idx, "campus access"))
        vl = [10, 20, access_vlans[k]]
        a.vlans = {v: (f"VLAN{v}", None) for v in vl}
        a.ifs.pop("GigabitEthernet0/0", None)
        a.vrfs.pop("Mgmt-vrf", None)
        svi(a, 10, f"10.{s.idx}.10.{10 + k}/24", r)
        a.statics.append(("0.0.0.0/0", f"10.{s.idx}.10.1", None))
        big = "big_trunk" if r.random() < 0.08 else None
        tvl = vl if not big else sorted(set(vl) | set(range(300, 3800, 7)))
        if big:
            a.quirks.add(big)
        if r.random() < 0.5 and len(a.free) > 8:
            m = [a.take_up(), a.take_up()]
            lag = make_lag(a, 1, m, tvl, "uplink to cores", r)
            for c, mp in zip(cores, m):
                cp = c.take()
                trunk(c, cp, tvl, f"to {a.name} {mp}", r)
        else:
            for c in cores:
                ap, cp = a.take_up(), c.take()
                trunk(a, ap, tvl, f"uplink {c.name}", r, native=999 if r.random() < 0.2 else None)
                trunk(c, cp, tvl, f"to {a.name}", r)
        access_ports(a, vl[1:], r, r.randrange(4, 30))


def dc_nxos(s: Site, n: int, bb: list[Dev], bbpool: Pool):
    r = s.rng
    modern = r.random() < 0.7
    if not modern:      # classic: N7K aggregation with HSRP SVIs, N5K access via vPC
        aggs = [s.add(new_dev(r, f"s{s.idx:03d}-agg{k}", "N7K", s.idx, "dc aggregation", asn=s.asn)) for k in (1, 2)]
        vl = [10] + [200 + k for k in range(n)]
        for a in aggs:
            loopback(a, next(s.lo))
            a.vlans = {v: (f"DC{v}", None) for v in vl}
        ibgp_pair(aggs[0], aggs[1], s.p2p, r, statics=False)
        for k, a in enumerate(aggs):
            p2p_bgp(a, bb[(s.idx + k) % len(bb)], bbpool, r)
            for v in vl:
                svi(a, v, f"10.{s.idx}.{v % 250}.{2 + k}/24", r, fhrp=("hsrp", v % 256, f"10.{s.idx}.{v % 250}.1"))
        for k in range(n):
            t = s.add(new_dev(r, f"s{s.idx:03d}-tor{k + 1:02d}", "N5K", s.idx, "dc access"))
            t.vlans = {v: (f"DC{v}", None) for v in (10, 200 + k)}
            svi(t, 10, f"10.{s.idx}.10.{20 + k}/24", r)
            m = [t.take_up(), t.take_up()]
            make_lag(t, 100, m, [10, 200 + k], "uplink to agg", r)
            for a in aggs:
                ap = a.take()
                trunk(a, ap, [10, 200 + k], f"to {t.name}", r)
            access_ports(t, [200 + k], r, r.randrange(4, 20))
        return
    spines = [s.add(new_dev(r, f"s{s.idx:03d}-spine{k}", "N9K", s.idx, "dc spine", asn=s.asn)) for k in (1, 2)]
    for sp in spines:
        loopback(sp, next(s.lo))
    leaves = []
    for k in range(n):
        lf = s.add(new_dev(r, f"s{s.idx:03d}-leaf{k + 1:02d}", pick(r, [("N9K", 5), ("N3K", 1)]), s.idx, "dc leaf",
                           asn=s.asn + 1000 + k if r.random() < 0.8 else s.asn))
        loopback(lf, next(s.lo))
        lf.ifs["loopback1"] = If("loopback1", "loopback", [f"172.17.{s.idx}.{k + 1}/32"])
        leaves.append(lf)
        for sp in spines:
            ibgp_like = lf.asn == sp.asn
            p2p_bgp(lf, sp, s.p2p, r, a_kw={"group": "UNDERLAY"}, b_kw={"group": "UNDERLAY", "rr": ibgp_like})
        for sp in spines:       # EVPN overlay over loopbacks
            session(lf, sp, lf.lo, sp.lo, r, {"upd": True, "mh": 3, "evpn": True, "group": "OVERLAY"},
                    {"upd": True, "mh": 3, "evpn": True, "group": "OVERLAY", "rr": lf.asn == sp.asn}, fault_rate=0.03)
        tenant = "TENANT1"
        lf.vrfs[tenant] = {"rd": "auto", "vni": 50000 + s.idx}
        lf.vlans = {1000 + v: (f"WEB{v}", 10000 + 100 * s.idx + v) for v in range(1, 4)}
        lf.vlans[3000] = ("L3VNI", 50000 + s.idx)
        for v in range(1, 4):
            svi(lf, 1000 + v, f"10.{s.idx}.{100 + v}.1/24", r, vrf=tenant)
        svi(lf, 3000, "", r, vrf=tenant)
        lf.ifs["Vlan3000"].ips = []
        if k % 2 == 1 and r.random() < 0.7:     # vPC pair with the previous leaf
            for x in (leaves[-2], lf):
                pl = [x.take_up(), x.take_up()]
                make_lag(x, 1, pl, None, "vpc peer-link", r, vpc="peer-link")
        access_ports(lf, [1001, 1002, 1003], r, r.randrange(2, 16))
    for k, sp in enumerate(spines):
        p2p_bgp(sp, bb[(s.idx + k) % len(bb)], bbpool, r)


def campus_junos(s: Site, n: int, bb: list[Dev], bbpool: Pool):
    r = s.rng
    core_model = pick(r, [("EX9208", 1), ("QFX5120", 2)])
    cores = [s.add(new_dev(r, f"s{s.idx:03d}-jcore{k}", core_model, s.idx, "campus core", asn=s.asn)) for k in (1, 2)]
    access_vlans = [100 + k for k in range(n)]
    for c in cores:
        loopback(c, next(s.lo))
        c.vlans = {v: (f"V{v}", None) for v in [10, 20] + access_vlans}
    ibgp_pair(cores[0], cores[1], s.p2p, r, statics=r.random() < 0.5)
    for k, c in enumerate(cores):
        p2p_bgp(c, bb[(s.idx + k) % len(bb)], bbpool, r)
        for v in [10, 20] + access_vlans:
            fh = ("vrrp", v % 256, f"10.{s.idx}.{v % 250}.1") if r.random() < 0.7 else None
            svi(c, v, f"10.{s.idx}.{v % 250}.{2 + k}/24", r, fhrp=fh)
    for k in range(n):
        model = pick(r, [("EX4200", 2), ("EX4300", 5)])
        a = s.add(new_dev(r, f"s{s.idx:03d}-jacc{k + 1:02d}", model, s.idx, "campus access"))
        vl = [10, 20, access_vlans[k]]
        a.vlans = {v: (f"V{v}", None) for v in vl}
        svi(a, 10, f"10.{s.idx}.10.{10 + k}/24", r)
        a.statics.append(("0.0.0.0/0", f"10.{s.idx}.10.1", None))
        m = [a.take_up(), a.take_up()]
        make_lag(a, 0, m, vl, "uplink to cores", r)
        for c in cores:
            trunk(c, c.take(), vl, f"to {a.name}", r)
        access_ports(a, vl[1:], r, r.randrange(4, 30))


def branch(s: Site, n: int, bb: list[Dev], bbpool: Pool, dyn: dict):
    r = s.rng
    model = pick(r, [("ISR2911", 3), ("ISR4451", 3), ("SRX1500", 2)])
    rt = s.add(new_dev(r, f"s{s.idx:03d}-rtr", model, s.idx, "branch router", asn=s.asn))
    loopback(rt, next(s.lo))
    hub = bb[s.idx % len(bb)]
    if hub.family == "ios" and rt.family == "ios" and r.random() < 0.4:     # dynamic neighbour on the hub
        rng_net = dyn.setdefault(hub.name, Pool(f"100.65.{len(dyn)}.0/24", 31))
        net = rng_net.next()
        ip_h, ip_r = str(net[0]), str(net[1])
        routed(hub, hub.take(), f"{ip_h}/31", f"to {rt.name}", r)
        routed(rt, rt.take(), f"{ip_r}/31", f"to {hub.name}", r)
        lst = f"100.65.{list(dyn).index(hub.name)}.0/24"
        if not any(p == lst for p, _, _ in hub.listen):
            hub.listen.append((lst, "BRANCHES", "65000-65535" if False else None))
        rt.nbrs.append(nbr(ip_h, hub.asn, hub.name))
    else:
        tag = 100 + s.idx if r.random() < 0.3 else None
        p2p_bgp(rt, hub, bbpool, r, tag=tag)
    if rt.family == "ios" and r.random() < 0.3:          # GRE tunnel with unnumbered and BGP across it
        t = If("Tunnel0", "tunnel", [f"10.240.{s.idx}.2/30"], desc=desc(rt, r, "GRE to DC"), tunnel_src=rt.ports[0])
        rt.ifs["Tunnel0"] = t
        rt.quirks.add("tunnel")
        rt.nbrs.append(nbr(f"10.240.{s.idx}.1", 64512, "tunnel head-end (not in snapshot)"))
    vl = [10, 20]
    lan = rt.take()
    for v in vl:
        routed(rt, lan, f"10.{s.idx}.{v}.1/24", f"LAN {v}", r, tag=v)
    for k in range(n):
        sw_model = pick(r, [("C2960", 2), ("C9300", 2), ("EX4300", 1)])
        sw = s.add(new_dev(r, f"s{s.idx:03d}-sw{k + 1}", sw_model, s.idx, "branch switch"))
        sw.vlans = {v: (f"VLAN{v}", None) for v in vl}
        sw.ifs.pop("GigabitEthernet0/0", None)
        sw.vrfs.pop("Mgmt-vrf", None)
        svi(sw, 10, f"10.{s.idx}.10.{10 + k}/24", r)
        sw.statics.append(("0.0.0.0/0", f"10.{s.idx}.10.1", None))
        trunk(sw, sw.take_up(), vl, "uplink", r)
        access_ports(sw, [20], r, r.randrange(4, 20))
    if k_trunk := (n > 0):
        rt.ifs[lan].mode = "routed-parent"


def pe_mx(s: Site, n: int, bb: list[Dev], bbpool: Pool):
    r = s.rng
    pe = s.add(new_dev(r, f"s{s.idx:03d}-pe", pick(r, [("MX960", 1), ("MX204", 1)]), s.idx, "provider edge", asn=s.asn))
    loopback(pe, next(s.lo))
    for k in range(2):
        p2p_bgp(pe, bb[(s.idx + k) % len(bb)], bbpool, r)
    for k in range(n):
        vrf = f"CUST-{chr(65 + k % 3)}"
        pe.vrfs.setdefault(vrf, {"rd": f"{pe.lo}:{k % 3 + 1}", "vni": None})
        ce = s.add(new_dev(r, f"s{s.idx:03d}-ce{k + 1:02d}", pick(r, [("ISR2911", 2), ("ISR4451", 2), ("ASR1001X", 1)]),
                           s.idx, "customer edge", asn=65500 + k % 3))
        loopback(ce, next(s.lo))
        port = pe.take() if k % 4 == 0 or not pe.free else pe.free[0]
        if k % 4 == 0:
            pe.ifs[port].mode = "routed-parent"
        p2p_bgp(pe, ce, s.p2p, r, vrf_a=vrf, tag=100 + k, a_port=port)


def inet_edge(s: Site, n: int, bb: list[Dev], bbpool: Pool):
    r = s.rng
    fw = s.add(new_dev(r, f"s{s.idx:03d}-fw", "SRX1500", s.idx, "internet firewall", asn=s.asn))
    rt = s.add(new_dev(r, f"s{s.idx:03d}-edge", "ASR1001X", s.idx, "internet router", asn=s.asn))
    for d in (fw, rt):
        loopback(d, next(s.lo))
    ibgp_pair(fw, rt, s.p2p, r)
    p2p_bgp(rt, bb[s.idx % len(bb)], bbpool, r)
    for k in range(max(1, n)):      # ISPs are not in the snapshot -> UNKNOWN_REMOTE
        net = ipaddress.IPv4Network(f"203.0.{113 - k % 2}.{4 * (s.idx % 60)}/31")
        p = routed(rt, rt.take(), f"{net[0]}/31", f"ISP{k + 1} transit", r)
        rt.acls.add("ACL-INTERNET-IN")
        rt.ifs[p].acl_in = "ACL-INTERNET-IN"
        rt.nbrs.append(nbr(str(net[1]), 65536 + k, f"ISP{k + 1}", rm_in="RM-ISP-IN", rm_out="RM-ISP-OUT"))
        rt.policies |= {"RM-ISP-IN", "RM-ISP-OUT"}


ARCHETYPES = [("campus_ios", 25, (4, 24)), ("dc_nxos", 18, (4, 18)), ("campus_junos", 18, (4, 22)),
              ("branch", 22, (0, 3)), ("pe_mx", 10, (2, 6)), ("inet_edge", 7, (1, 3))]


def build(count: int, rng: random.Random) -> list[Dev]:
    bb = []
    bbpool = Pool("100.64.16.0/20", 31)
    core = Pool("100.64.0.0/22", 31)
    for k in range(8):
        model = "ASR1001X" if k < 4 else pick(rng, [("MX960", 1), ("MX204", 1)])
        d = new_dev(rng, f"bb-{k + 1}", model, 0, "backbone", asn=64512)
        d.quirks -= {"no_hostname", "latin1"}
        loopback(d, f"172.16.0.{k + 1}")
        mgmt(d, f"192.168.0.{k + 10}")
        # backbone has more ports than the models list; give every backbone router a large port pool
        extra = [f"{'TenGigabitEthernet0/2/' if model == 'ASR1001X' else 'xe-3/0/'}{n}" for n in range(0, 200)]
        d.ports += extra
        d.free = d.ports[:] if model != "ASR1001X" else d.ports[:]
        for p in extra:
            d.ifs[p] = If(p)
        d.ospf = True
        bb.append(d)
    for i in range(8):
        for j in range(i + 1, 8):
            net = core.next()
            routed(bb[i], bb[i].take_up(), f"{net[0]}/31", f"core to {bb[j].name}", rng)
            routed(bb[j], bb[j].take_up(), f"{net[1]}/31", f"core to {bb[i].name}", rng)
            session(bb[i], bb[j], bb[i].lo, bb[j].lo, rng, {"upd": True, "group": "IBGP", "local": bb[i].lo},
                    {"upd": True, "group": "IBGP", "local": bb[j].lo}, fault_rate=0.0)
    devs = list(bb)
    dyn: dict = {}
    idx = 1
    budget = count - len(devs)
    while budget > 0:
        kind = pick(rng, [(a, w) for a, w, _ in ARCHETYPES])
        lo, hi = dict((a, rng_) for a, _, rng_ in ARCHETYPES)[kind]
        n = rng.randint(lo, hi)
        s = Site(idx, rng)
        globals()[kind](s, n, bb, bbpool, dyn) if kind == "branch" else globals()[kind](s, n, bb, bbpool)
        if len(s.devs) > budget:        # trim the last site to land on the exact count
            keep = s.devs[:budget]
            gone = {d.name for d in s.devs[budget:]}
            for d in keep + bb:
                d.nbrs = [x for x in d.nbrs if x["desc"] not in gone]
            s.devs = keep
        devs += s.devs
        budget -= len(s.devs)
        idx += 1
    return devs


# ---------------------------------------------------------------------------------------------- render helpers
def mask(plen: int) -> str:
    return str(ipaddress.IPv4Network(f"0.0.0.0/{plen}").netmask)


def ip_mask(cidr: str) -> str:
    a, p = cidr.split("/")
    return f"{a} {mask(int(p))}"


def vlan_ranges(v) -> str:
    v = sorted(set(v))
    out, start, prev = [], v[0], v[0]
    for x in v[1:] + [None]:
        if x is not None and x == prev + 1:
            prev = x
            continue
        out.append(str(start) if start == prev else f"{start}-{prev}")
        if x is not None:
            start = prev = x
    return ",".join(out)


def wrap_vlans(prefix: str, text: str, width: int = 70) -> list[str]:
    """IOS breaks long 'allowed vlan' lists into a first line and 'add' lines."""
    parts, lines, cur = text.split(","), [], ""
    for p in parts:
        if cur and len(cur) + len(p) + 1 > width:
            lines.append(cur)
            cur = p
        else:
            cur = f"{cur},{p}" if cur else p
    lines.append(cur)
    return [f"{prefix} {lines[0]}"] + [f"{prefix} add {x}" for x in lines[1:]]


def lag_number(name: str) -> str:
    return re.sub(r"\D", "", name)


def short_version(v: str) -> str:
    m = re.match(r"(\d+)\.(\d+)", v)
    return f"{m.group(1)}.{m.group(2)}"


BANNER_IOS = ["^C", "*** Authorised access only ***", "interface GigabitEthernet9/9", " ip address 1.2.3.4 255.255.255.0",
              "!", "router bgp 1", "^C"]


# ---------------------------------------------------------------------------------------------- IOS
def render_ios(d: Dev, rng) -> str:
    v = d.version
    old = v.startswith("12.")
    xe = v.startswith(("16.", "17."))
    sw = d.kind == "switch"
    host = d.name.upper() if "uppercase_host" in d.quirks else d.name
    L = []
    if "rancid" in d.quirks:
        L += ["!RANCID-CONTENT-TYPE: cisco", "!"]
    L += ["Building configuration...", "", f"Current configuration : {rng.randrange(8000, 90000)} bytes", "!",
          "! Last configuration change at 09:00:00 AEDT Thu Nov 19 2026 by netops", "!",
          f"version {short_version(v)}", "service timestamps debug datetime msec",
          "service timestamps log datetime msec", "service password-encryption", "!"]
    if "no_hostname" not in d.quirks:
        L += [f"hostname {host}", "!"]
    L += ["boot-start-marker", "boot system flash:packages.conf" if xe else f"boot system flash:{d.model.lower()}-mz.bin",
          "boot-end-marker", "!"]
    for name, vr in d.vrfs.items():
        if old:
            L += [f"ip vrf {name}"] + ([f" rd {vr['rd']}"] if vr["rd"] else []) + ["!"]
        else:
            L += [f"vrf definition {name}"] + ([f" rd {vr['rd']}"] if vr["rd"] else [])
            L += [" !", " address-family ipv4", " exit-address-family", "!"]
    L += ["no aaa new-model" if old else "aaa new-model", "!"]
    if sw or d.kind == "router":
        L += ["ip routing", "!"] if sw and d.lo else []
    L += ["no ip domain-lookup" if old else "no ip domain lookup", "ip domain name lab.example.net", "!"]
    if "cert" in d.quirks and not old:
        L += ["crypto pki trustpoint TP-self-signed-1234567", " enrollment selfsigned", " revocation-check none", "!",
              "crypto pki certificate chain TP-self-signed-1234567", " certificate self-signed 01",
              "  3082032E 30820216 A0030201 02020101 300D0609 2A864886 F70D0101 05050030",
              "  interface GigabitEthernet9/9 0101 0101 0101 0101", "  \tquit", "!"]
    stp = []
    if sw:
        stp = [f"spanning-tree mode {'pvst' if old else 'rapid-pvst'}", "spanning-tree extend system-id"]
        if "portfast_default" in d.quirks:
            stp.append("spanning-tree portfast default" if not xe else "spanning-tree portfast edge default")
    vl = []
    if sw and d.vlans:
        if old:
            vl.append("vlan internal allocation policy ascending")
            vl.append("!")
        for vid, (vname, _) in sorted(d.vlans.items()):
            vl += [f"vlan {vid}", f" name {vname}", "!"]
    odd = "odd_order" in d.quirks
    if not odd:
        L += stp + ["!"] + vl
    for a in sorted(d.acls):
        if a.startswith("ACL-UNDEF"):
            continue
        L += [f"ip access-list extended {a}", " 10 deny ip 10.0.0.0 0.255.255.255 any", " 20 permit ip any any", "!"]
    for p in sorted(d.policies):
        if "undefined_policy" in d.quirks and p.endswith("-IN"):
            continue
        L += [f"route-map {p} permit 10", "!"]
    order = sorted(d.ifs.values(), key=lambda i: (
        0 if i.name.startswith("Loopback") else 1 if i.name.startswith("Tunnel") else
        2 if i.name.startswith("Port-channel") else 4 if i.name.startswith("Vlan") else 3, d.ports.index(i.name.split(".")[0])
        if i.name.split(".")[0] in d.ports else 0, i.name))
    first_if = True
    for i in order:
        L += render_ios_if(d, i, rng, old, xe, sw)
        if first_if and odd:
            L += vl + stp + ["!"]      # vlan stanzas straight after an interface, spanning-tree after a vlan
            first_if = False
    if d.ospf:
        L += ["router ospf 1", f" router-id {d.lo}", " passive-interface default"]
        L += [f" no passive-interface {i.name}" for i in d.ifs.values() if i.mode == "routed" and i.vrf is None][:6]
        L += [" network 100.64.0.0 0.0.255.255 area 0", f" network 172.16.0.0 0.0.255.255 area 0",
              f" network 10.{d.site}.250.0 0.0.0.255 area 0", "!"]
    if d.nbrs or d.listen:
        L += render_ios_bgp(d, rng, old)
    for pfx, nh, vrf in d.statics:
        net = ipaddress.IPv4Network(pfx)
        L.append(f"ip route{' vrf ' + vrf if vrf else ''} {net.network_address} {net.netmask} {nh}")
    if d.vrfs.get("Mgmt-vrf") or d.vrfs.get("Mgmt-intf"):
        m = next(i for i in d.ifs.values() if i.mode == "mgmt")
        gw = str(ipaddress.IPv4Interface(m.ips[0]).network[1])
        L.append(f"ip route vrf {m.vrf} 0.0.0.0 0.0.0.0 {gw}")
    L += ["ip forward-protocol nd", "no ip http server", "!"]
    if "banner" in d.quirks:
        L += ["banner motd " + BANNER_IOS[0]] + BANNER_IOS[1:]
    L += ["!", "line con 0", " exec-timeout 15 0", "line vty 0 4", " transport input ssh", "!", "end"]
    return "\n".join(L) + "\n"


def render_ios_if(d: Dev, i: If, rng, old, xe, sw) -> list[str]:
    out = [f"interface {i.name}"]
    if i.desc:
        out.append(f" description {i.desc}")
    physical = i.name in d.ports
    if i.mode == "unused":
        out += [" no ip address", " shutdown"] if d.kind == "router" else ([" shutdown"] if rng.random() < 0.3 else [])
    elif i.mode == "routed-parent":
        out += [" no ip address"] + ([" no switchport"] if sw else [])
    elif i.mode in ("access", "trunk", "lag-member") and not (i.mode == "lag-member" and not sw):
        src = d.ifs[i.lag] if i.mode == "lag-member" else i
        if src.mode == "trunk":
            if old and d.model in ("C3750", "C6509"):
                out.append(" switchport trunk encapsulation dot1q")
            if src.native:
                out.append(f" switchport trunk native vlan {src.native}")
            if src.vlans is not None:
                out += wrap_vlans(" switchport trunk allowed vlan", vlan_ranges(src.vlans))
            out.append(" switchport mode trunk")
        else:
            out += [f" switchport access vlan {i.vlan}", " switchport mode access"]
            if 20 in d.vlans and rng.random() < 0.4:
                out.append(" switchport voice vlan 20")
            if rng.random() < 0.5:
                out.append(" spanning-tree portfast" if old else " spanning-tree portfast edge")
        if i.mode == "lag-member":
            out.append(f" channel-group {lag_number(i.lag)} mode active")
    else:
        if sw and physical:
            out.append(" no switchport")
        if i.vlan is not None and "." in i.name:
            out.append(f" encapsulation dot1Q {i.vlan}")
        if i.vrf:
            out.append(f" {'ip vrf forwarding' if old else 'vrf forwarding'} {i.vrf}")
        if i.unnumbered:
            out.append(f" ip unnumbered {i.unnumbered}")
        for k, a in enumerate(i.ips):
            out.append(f" ip address {ip_mask(a)}{' secondary' if k else ''}")
        if not i.ips and i.mode not in ("tunnel",):
            out.append(" no ip address")
        for h in i.helpers:
            out.append(f" ip helper-address {h}")
        if i.acl_in:
            ref = i.acl_in if "undefined_acl" not in d.quirks else "ACL-UNDEF-IN"
            out.append(f" ip access-group {ref} in")
        if i.fhrp:
            kind, g, vip = i.fhrp
            if kind == "hsrp":
                out += ([" standby version 2"] if not old else []) + [f" standby {g} ip {vip}", f" standby {g} priority 110",
                                                                      f" standby {g} preempt"]
            elif xe:
                out += [f" vrrp {g} address-family ipv4", f"  address {vip} primary"]
            else:
                out += [f" vrrp {g} ip {vip}"]
        if i.mode == "routed" and rng.random() < 0.3:
            out.append(" no ip proxy-arp")
        if i.mode == "tunnel":
            out += [f" tunnel source {i.tunnel_src}", " tunnel destination 198.51.100.1"]
        if i.mtu:
            out.append(f" mtu {i.mtu}")
    if i.shut and i.mode != "unused":
        out.append(" shutdown")
    if "trailing_ws" in d.quirks:
        out = [x + "  " for x in out]
    return out + ["!"]


def asn_text(d: Dev, asn: int) -> str:
    if "asdot" in d.quirks and asn > 65535:
        return f"{asn >> 16}.{asn & 0xFFFF}"
    return str(asn)


def render_ios_bgp(d: Dev, rng, old) -> list[str]:
    if d.asn and d.asn > 65535 and rng.random() < 0.3:
        d.quirks.add("asdot")
    L = [f"router bgp {asn_text(d, d.asn)}"]
    if "asdot" in d.quirks:
        L.append(" bgp asnotation dot")
    if not old and d.lo:
        L.append(f" bgp router-id {d.lo}")
    L.append(" bgp log-neighbor-changes")
    nodef = "no_default_ipv4" in d.quirks
    if nodef:
        L.append(" no bgp default ipv4-unicast")
    for pfx, grp, _ in d.listen:
        L += [f" bgp listen range {pfx} peer-group {grp}", " bgp listen limit 200"]
    groups = sorted({n["group"] for n in d.nbrs if n.get("group") == "IBGP" and not n.get("vrf")})
    if d.listen:
        L += [" neighbor BRANCHES peer-group", " neighbor BRANCHES remote-as 65000 alternate-as 65001 65002",
              " neighbor BRANCHES description dynamic branches"]
    for g in groups:
        L += [f" neighbor {g} peer-group", f" neighbor {g} remote-as {asn_text(d, d.asn)}",
              f" neighbor {g} update-source {names(d)['lo']}"]
    glob = [n for n in d.nbrs if not n.get("vrf")]
    for n in glob:
        ip = n["ip"]
        if n.get("group") in groups:
            L.append(f" neighbor {ip} peer-group {n['group']}")
        elif n["asn"] is not None:
            L.append(f" neighbor {ip} remote-as {asn_text(d, n['asn'])}")
        L.append(f" neighbor {ip} description {n['desc']}")
        if n.get("upd") is False and n.get("group") in groups:
            pass
        if n.get("mh"):
            L.append(f" neighbor {ip} ebgp-multihop {n['mh']}")
        if n.get("shut"):
            L.append(f" neighbor {ip} shutdown")
        if old:
            if n.get("rm_in"):
                L.append(f" neighbor {ip} route-map {n['rm_in']} in")
    if "v6_peer" in d.quirks:
        L += [" neighbor 2001:DB8::1 remote-as 65530", " neighbor 2001:DB8::1 description v6 transit"]
    if old:
        L += [" no auto-summary", "!"]
    else:
        L += [" !", " address-family ipv4"]
        if d.lo:
            L.append(f"  network {d.lo} mask 255.255.255.255")
        for g in groups:
            L += [f"  neighbor {g} send-community both"]
        for n in glob:
            if nodef and rng.random() < 0.1:
                d.faults.append(f"not_activated: {d.name} -> {n['ip']}")
                continue
            L.append(f"  neighbor {n['ip']} activate")
            if n.get("rr"):
                L.append(f"  neighbor {n['ip']} route-reflector-client")
            if n.get("rm_in"):
                L.append(f"  neighbor {n['ip']} route-map {n['rm_in']} in")
            if n.get("rm_out"):
                L.append(f"  neighbor {n['ip']} route-map {n['rm_out']} out")
        L += ["  maximum-paths 4", " exit-address-family", " !"]
        if "v6_peer" in d.quirks:
            L += [" address-family ipv6", "  neighbor 2001:DB8::1 activate", " exit-address-family", " !"]
    for vrf in sorted({n["vrf"] for n in d.nbrs if n.get("vrf")}):
        L.append(f" address-family ipv4 vrf {vrf}")
        for n in d.nbrs:
            if n.get("vrf") == vrf:
                L += [f"  neighbor {n['ip']} remote-as {asn_text(d, n['asn'])}" if n["asn"] else f"  neighbor {n['ip']} activate",
                      f"  neighbor {n['ip']} activate"]
        L.append(" exit-address-family")
    return L + ["!"]


# ---------------------------------------------------------------------------------------------- NX-OS
def render_nxos(d: Dev, rng) -> str:
    v = d.version
    host = d.name.upper() if "uppercase_host" in d.quirks else d.name
    L = []
    if "rancid" in d.quirks:
        L += ["!RANCID-CONTENT-TYPE: cisco-nx", "!"]
    L += ["!Command: show running-config", "!Running configuration last done at: Thu Nov 19 09:00:00 2026",
          "!Time: Thu Nov 19 09:05:00 2026", ""]
    bios = {"N9K": " Bios:version 05.47", "N3K": " Bios:version 5.0.0"}.get(d.model, "")
    L += [f"version {v}{bios}"]
    if d.model == "N5K":
        L += [f"switchname {host}"]
    else:
        L += [f"hostname {host}"]
    feats = ["bgp" if d.nbrs else None, "interface-vlan" if any(i.mode == "svi" for i in d.ifs.values()) else None,
             "lacp", "vpc" if any(i.vpc for i in d.ifs.values()) else None,
             "hsrp" if any(i.fhrp for i in d.ifs.values()) else None, "ospf" if d.ospf else None]
    if any(vn for _, vn in d.vlans.values()):
        feats += ["nv overlay", "vn-segment-vlan-based"]
        L += ["nv overlay evpn"]
    L += [f"feature {f}" for f in feats if f]
    L += [""]
    if "banner" in d.quirks:
        L += ["banner motd #", "Authorised access only", "interface Ethernet9/9", "#", ""]
    if d.model == "N7K":
        tag = v.replace("(", ".").replace(")", "")
        L += [f"boot kickstart bootflash:/n7000-s2-kickstart.{tag}.bin sup-1",
              f"boot system bootflash:/n7000-s2-dk9.{tag}.bin sup-1"]
    elif d.model == "N5K":
        L += ["boot kickstart bootflash:/n5000-uk9-kickstart.7.3.8.N1.1.bin", "boot system bootflash:/n5000-uk9.7.3.8.N1.1.bin"]
    else:
        L += [f"boot nxos bootflash:/nxos{'64-cs' if v.startswith('10') else ''}.{v.replace('(', '.').replace(')', '')}.bin"]
    L += ["", "ip domain-lookup", "ip domain-name lab.example.net", ""]
    if any(vn for _, vn in d.vlans.values()):
        L += ["fabric forwarding anycast-gateway-mac 0000.2222.3333"]
    if d.vlans:
        L += [f"vlan {vlan_ranges([1] + list(d.vlans))}"]
        for vid, (vname, vni) in sorted(d.vlans.items()):
            L += [f"vlan {vid}", f"  name {vname}"] + ([f"  vn-segment {vni}"] if vni else [])
        L += [""]
    for p in sorted(d.policies):
        L += [f"route-map {p} permit 10"]
    for name, vr in d.vrfs.items():
        L += [f"vrf context {name}"]
        if vr.get("vni"):
            L += [f"  vni {vr['vni']}", "  rd auto", "  address-family ipv4 unicast",
                  "    route-target both auto", "    route-target both auto evpn"]
    m = d.ifs.get("mgmt0")
    L += ["vrf context management", f"  ip route 0.0.0.0/0 {ipaddress.IPv4Interface(m.ips[0]).network[1]}", ""]
    if any(i.vpc for i in d.ifs.values()):
        L += ["vpc domain 10", "  peer-switch", "  peer-keepalive destination 192.168.0.1 source 192.168.0.2 vrf management",
              ""]
    for i in d.ifs.values():
        L += render_nxos_if(d, i, rng)
    nve = any(vn for _, vn in d.vlans.values())
    if nve:
        L += ["interface nve1", "  no shutdown", "  host-reachability protocol bgp", "  source-interface loopback1"]
        for vid, (_, vni) in sorted(d.vlans.items()):
            if vni and vid != 3000:
                L += [f"  member vni {vni}", "    ingress-replication protocol bgp"]
        for name, vr in d.vrfs.items():
            if vr.get("vni"):
                L += [f"  member vni {vr['vni']} associate-vrf"]
        L += [""]
    if d.ospf:
        L += ["router ospf 1", f"  router-id {d.lo}", ""]
    if d.nbrs:
        L += render_nxos_bgp(d, rng)
    for pfx, nh, vrf in d.statics:
        L.append(f"ip route {pfx} {nh}")
    L += ["", "line console", "line vty", ""]
    return "\n".join(L) + "\n"


def render_nxos_if(d: Dev, i: If, rng) -> list[str]:
    out = [f"interface {i.name}"]
    l3_default = d.model in ("N7K", "N9K") and not d.version.startswith("7.0(3)I7") or d.model == "N7K"
    if i.desc:
        out.append(f"  description {i.desc}")
    if i.mode == "unused":
        return out + ([""] if rng.random() < 0.5 else ["  shutdown", ""])
    if i.mode in ("trunk", "access", "lag-member"):
        src = d.ifs[i.lag] if i.mode == "lag-member" else i
        if l3_default or rng.random() < 0.5:
            out.append("  switchport")
        if src.mode == "trunk":
            out.append("  switchport mode trunk")
            if src.native:
                out.append(f"  switchport trunk native vlan {src.native}")
            if src.vlans is not None:
                out.append(f"  switchport trunk allowed vlan {vlan_ranges(src.vlans)}")
            if i.vpc:
                out.append(f"  vpc {i.vpc}")
        else:
            out += [f"  switchport access vlan {i.vlan}", "  spanning-tree port type edge"]
        if i.mode == "lag-member":
            out.append(f"  channel-group {lag_number(i.lag)} mode active")
        out.append("  no shutdown" if not i.shut else "  shutdown")
        return out + [""]
    if i.mode == "routed-parent":
        return out + ["  no switchport", "  no shutdown", ""]
    if i.name in d.ports and (not l3_default or rng.random() < 0.6):
        out.append("  no switchport")
    if i.vlan is not None and "." in i.name:
        out.append(f"  encapsulation dot1q {i.vlan}")
    if i.mtu or (i.name in d.ports and rng.random() < 0.5):
        out.append(f"  mtu {i.mtu or 9216}")
    if i.vrf:
        out.append(f"  vrf member {i.vrf}")
    if i.mode == "svi" and i.vlan == 3000:
        out.append("  ip forward")
    for k, a in enumerate(i.ips):
        out.append(f"  ip address {a}{' secondary' if k else ''}")
    if i.mode == "svi" and i.vrf == "TENANT1" and i.ips:
        out.append("  fabric forwarding mode anycast-gateway")
    if i.fhrp:
        out += ["  hsrp version 2", f"  hsrp {i.fhrp[1]}", f"    ip {i.fhrp[2]}"]
    if i.mode != "mgmt":
        out.append("  shutdown" if i.shut else "  no shutdown")
    return out + [""]


def render_nxos_bgp(d: Dev, rng) -> list[str]:
    L = [f"router bgp {d.asn}", f"  router-id {d.lo}", "  log-neighbor-changes", "  address-family ipv4 unicast",
         f"    network {d.lo}/32", "    maximum-paths 4"]
    if any(n.get("evpn") for n in d.nbrs):
        L += ["  address-family l2vpn evpn", "    retain route-target all"]
    tmpl = sorted({n["group"] for n in d.nbrs if n.get("group") in ("OVERLAY", "IBGP")})
    for t in tmpl:
        ex = next(n for n in d.nbrs if n.get("group") == t)
        L += [f"  template peer {t}"]
        if t == "IBGP":
            L += [f"    remote-as {d.asn}"]
        L += ["    update-source loopback0"]
        if ex.get("mh"):
            L += [f"    ebgp-multihop {ex['mh']}"]
        if ex.get("evpn"):
            L += ["    address-family l2vpn evpn", "      send-community", "      send-community extended"]
        else:
            L += ["    address-family ipv4 unicast", "      send-community"]
    for n in d.nbrs:
        if n.get("vrf"):
            continue
        L += [f"  neighbor {n['ip']}"]
        if n.get("group") in tmpl:
            L += [f"    inherit peer {n['group']}"]
        if n["asn"] is not None and not (n.get("group") == "IBGP"):
            L += [f"    remote-as {n['asn']}"]
        L += [f"    description {n['desc']}"]
        if n.get("shut"):
            L += ["    shutdown"]
        if n.get("group") not in tmpl:
            L += ["    address-family ipv4 unicast"]
            if n.get("rr"):
                L += ["      route-reflector-client"]
            if n.get("rm_out"):
                L += [f"      route-map {n['rm_out']} out"]
        elif n.get("rr"):
            L += ["    address-family l2vpn evpn", "      route-reflector-client"]
    if "v6_peer" in d.quirks:
        L += ["  neighbor 2001:db8::1", "    remote-as 65530", "    address-family ipv6 unicast"]
    for name, vr in d.vrfs.items():
        if vr.get("vni"):
            L += [f"  vrf {name}", "    address-family ipv4 unicast", "      advertise l2vpn evpn"]
    return L + [""]


# ---------------------------------------------------------------------------------------------- Junos
def junos_statements(d: Dev, rng) -> tuple[list[list[str]], list[list[str]]]:
    """Return (set statements as token lists, paths to deactivate)."""
    S: list[list[str]] = []
    inactive: list[list[str]] = []
    host = d.name.upper() if "uppercase_host" in d.quirks else d.name
    S += [["version", d.version], ["system", "host-name", host], ["system", "domain-name", "lab.example.net"],
          ["system", "name-server", "10.255.5.1"], ["system", "ntp", "server", "10.255.1.1"],
          ["system", "syslog", "host", "10.255.2.1", "any", "notice"]]
    if "banner" in d.quirks:
        S.append(["system", "login", "message", "Authorised access only\\n set interfaces ge-9/9/9 disable"])
    if "apply_groups" in d.quirks:
        S += [["groups", "JUMBO", "interfaces", "<*-0/0/*>", "mtu", "9192"], ["apply-groups", "JUMBO"]]
    lags = [i for i in d.ifs.values() if i.name.startswith("ae") and "." not in i.name]
    if lags:
        S.append(["chassis", "aggregated-devices", "ethernet", "device-count", str(len(lags) + 2)])
    vlan_name = {vid: vn for vid, (vn, _) in d.vlans.items()}
    es = ["family", "ethernet-switching"]
    rng_ports = []
    if "interface_range" in d.quirks:
        rng_ports = [i for i in d.ifs.values() if i.mode == "access" and i.name.startswith("ge-0/0/")][:12]
        if len(rng_ports) >= 2:
            vid = rng_ports[0].vlan
            rng_ports = [p for p in rng_ports if p.vlan == vid]
        if len(rng_ports) >= 2:
            S += [["interfaces", "interface-range", "USERS", "member-range", rng_ports[0].name, "to", rng_ports[-1].name]]
            mode = "interface-mode" if d.els else "port-mode"
            S += [["interfaces", "interface-range", "USERS", "unit", "0", *es, mode, "access"],
                  ["interfaces", "interface-range", "USERS", "unit", "0", *es, "vlan", "members", vlan_name[vid]]]
            covered = {p.name for p in d.ifs.values() if p.name.startswith("ge-0/0/")
                       and int(p.name.split("/")[-1]) in range(int(rng_ports[0].name.split("/")[-1]),
                                                               int(rng_ports[-1].name.split("/")[-1]) + 1)}
            rng_ports = covered
        else:
            rng_ports = set()
    for i in d.ifs.values():
        base, unit = (i.name.split(".") + ["0"])[:2]
        P = ["interfaces", base]
        if i.mode == "unused":
            continue
        if i.mode == "routed-parent":
            S.append(P + ["vlan-tagging"])
            continue
        if i.desc and "." not in i.name:
            S.append(P + ["description", i.desc])
        if i.shut:
            S.append(P + ["disable"])
        if i.mode == "lag-member":
            opt = "ether-options" if d.model.startswith(("EX", "QFX")) else "gigether-options"
            S.append(P + [opt, "802.3ad", i.lag])
            continue
        if i.name.startswith("ae") and "." not in i.name:
            S.append(P + ["aggregated-ether-options", "lacp", "active"])
        U = P + ["unit", unit]
        if i.desc and "." in i.name:
            S.append(U + ["description", i.desc])
        if i.vlan is not None and "." in i.name and i.mode == "routed":
            S.append(U + ["vlan-id", str(i.vlan)])
        if i.mode in ("routed", "loopback", "mgmt", "svi"):
            for k, a in enumerate(i.ips):
                S.append(U + ["family", "inet", "address", a])
                if i.fhrp and k == 0:
                    S += [U + ["family", "inet", "address", a, "vrrp-group", str(i.fhrp[1]), "virtual-address", i.fhrp[2]]]
            if i.mode == "loopback" and "undefined_policy" not in d.quirks:
                S.append(U + ["family", "inet", "filter", "input", "PROTECT-RE"])
        elif i.mode in ("trunk", "access") and i.name not in rng_ports:
            mode = "interface-mode" if d.els else "port-mode"
            S.append(U + es + [mode, i.mode])
            members = [vlan_name.get(v, str(v)) for v in (i.vlans if i.vlans is not None else [])] \
                if i.mode == "trunk" else [vlan_name.get(i.vlan, str(i.vlan))]
            if i.mode == "trunk" and i.vlans is None:
                members = ["all"]
            S.append(U + es + ["vlan", "members", members if len(members) > 1 else members[0]])
            if i.native:
                S.append((P if d.els else U + es) + ["native-vlan-id", str(i.native)])
        if "inactive" in d.quirks and i.mode == "access" and rng.random() < 0.1:
            inactive.append(P)
    for vid, (vn, vni) in sorted(d.vlans.items()):
        S.append(["vlans", vn, "vlan-id", str(vid)])
        l3 = names(d)["svi"](vid)
        if l3 in d.ifs:
            S.append(["vlans", vn, "l3-interface", l3])
    if any(i.mode == "loopback" for i in d.ifs.values()):
        S += [["firewall", "family", "inet", "filter", "PROTECT-RE", "term", "SSH", "then", "accept"]]
    for name, vr in d.vrfs.items():
        R = ["routing-instances", name]
        S += [R + ["instance-type", "vrf"], R + ["route-distinguisher", vr["rd"] or f"{d.lo}:1"],
              R + ["vrf-target", "target:64512:1"]]
        for i in d.ifs.values():
            if i.vrf == name:
                S.append(R + ["interface", i.name])
    if d.model.startswith("SRX"):
        for i in d.ifs.values():
            if i.mode == "routed":
                S.append(["security", "zones", "security-zone", "trust", "interfaces", i.name])
    for pfx, nh, vrf in d.statics:
        S.append(["routing-options", "static", "route", pfx, "next-hop", nh])
    if d.lo:
        S += [["routing-options", "router-id", d.lo]]
    if d.asn:
        S += [["routing-options", "autonomous-system", str(d.asn)]]
    if d.ospf:
        S += [["protocols", "ospf", "area", "0.0.0.0", "interface", "lo0.0", "passive"]]
        S += [["protocols", "ospf", "area", "0.0.0.0", "interface", i.name] for i in d.ifs.values()
              if i.mode == "routed" and i.vrf is None][:8]
    S += [["policy-options", "policy-statement", "EXPORT-LO", "term", "1", "from", "protocol", "direct"],
          ["policy-options", "policy-statement", "EXPORT-LO", "term", "1", "then", "accept"]]
    for n in d.nbrs:
        G = ["protocols", "bgp", "group", n.get("group") or ("EBGP" if n["asn"] != d.asn else "IBGP")]
        if n.get("vrf"):
            G = ["routing-instances", n["vrf"]] + G[0:2] + ["group", "CE"]
        ibgp = n.get("group") == "IBGP"
        S.append(G + ["type", "internal" if ibgp else "external"])
        if not ibgp:
            pol = "EXPORT-LO" if "undefined_policy" not in d.quirks else "EXPORT-MISSING"
            S += [G + ["export", pol], G + ["multipath", "multiple-as"]] if rng.random() < 0.5 else [G + ["export", pol]]
        N = G + ["neighbor", n["ip"]]
        S.append(N + ["description", n["desc"]])
        if n["asn"] is not None and not ibgp:
            S.append(N + ["peer-as", str(n["asn"])])
        if n.get("local") and ("no_local_address" not in d.quirks or ibgp):
            S.append((G if ibgp else N) + ["local-address", n["local"]])
        if n.get("mh"):
            S.append(N + ["multihop", "ttl", str(n["mh"])])
        if n.get("evpn"):
            S.append(N + ["family", "evpn", "signaling"])
        if n.get("rr"):
            S.append(G + ["cluster", d.lo])
        if n.get("shut"):
            inactive.append(N)
    if "v6_peer" in d.quirks:
        S += [["protocols", "bgp", "group", "V6", "type", "external"],
              ["protocols", "bgp", "group", "V6", "neighbor", "2001:db8::1", "peer-as", "65530"]]
    # one statement per member for list-valued leaves; brace format folds them back into [ ... ]
    out = []
    for s in S:
        if isinstance(s[-1], list):
            out += [s[:-1] + [m] for m in s[-1]]
        else:
            out.append(s)
    return out, inactive


def q(tok: str) -> str:
    if re.search(r'[\s;{}\[\]"#]', tok) or tok == "":
        return '"' + tok.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return tok


NAMED = {"interfaces", "unit", "group", "neighbor", "vlans", "routing-instances", "policy-statement", "term",
         "filter", "security-zone", "groups", "interface-range", "area", "route", "family", "server", "host"}
LISTS = {"members", "apply-groups"}


def junos_brace(stmts: list[list[str]], inactive: list[list[str]]) -> str:
    """Curly-brace rendering, Junos style: 'unit 0 {', 'members [ A B ];', 'inactive: ...'."""
    tree: dict = {}
    for s in stmts:
        node = tree
        for t in s:
            node = node.setdefault(t, {})
        node[None] = True                 # statement ends here
    inact = {tuple(p) for p in inactive}

    def emit(node: dict, path: tuple, depth: int) -> list[str]:
        pad, out = "    " * depth, []
        for tok, child in node.items():
            if tok is None:
                continue
            p = path + (tok,)
            pre = "inactive: " if p in inact else ""
            kids = [k for k in child if k is not None]
            if not kids:
                out.append(f"{pad}{pre}{q(tok)};")
            elif tok in LISTS and all(not [k for k in child[x] if k is not None] for x in kids):
                out.append(f"{pad}{pre}{tok} {'[ ' + ' '.join(q(k) for k in kids) + ' ]' if len(kids) > 1 else q(kids[0])};")
            elif len(kids) == 1 and not [k for k in child[kids[0]] if k is not None] and tok not in NAMED:
                out.append(f"{pad}{pre}{tok} {q(kids[0])};")
            elif tok == "member-range" and len(kids) == 1:          # member-range ge-0/0/0 to ge-0/0/23;
                chain, sub = [kids[0]], child[kids[0]]
                while [k for k in sub if k is not None]:
                    nk = [k for k in sub if k is not None][0]
                    chain.append(nk)
                    sub = sub[nk]
                out.append(f"{pad}{pre}{tok} {' '.join(q(c) for c in chain)};")
            elif tok in NAMED and path and (path[-1] not in NAMED or tok == "interface-range"):
                for name in kids:
                    sub = child[name]
                    pre2 = "inactive: " if p + (name,) in inact else pre
                    if not [k for k in sub if k is not None]:
                        out.append(f"{pad}{pre2}{tok} {q(name)};")
                        continue
                    out.append(f"{pad}{pre2}{tok} {q(name)} {{")
                    out += emit(sub, p + (name,), depth + 1)
                    out.append(f"{pad}}}")
            else:
                out.append(f"{pad}{pre}{q(tok)} {{")
                out += emit(child, p, depth + 1)
                out.append(f"{pad}}}")
        return out

    ver = next((s[1] for s in stmts if s[0] == "version"), "")
    body = [s for s in stmts if s[0] != "version"]
    tree.pop("version", None)
    return "\n".join([f"## Last commit: 2026-11-19 09:00:00 AEDT by netops", f"version {ver};"] +
                     emit(tree, (), 0)) + "\n"


def render_junos(d: Dev, rng) -> str:
    stmts, inactive = junos_statements(d, rng)
    head = ["# RANCID-CONTENT-TYPE: juniper", "#"] if "rancid" in d.quirks else []
    if d.fmt == "set":
        lines = ["set " + " ".join(q(t) for t in s) for s in stmts]
        lines += ["deactivate " + " ".join(q(t) for t in p) for p in inactive]
        return "\n".join(head + lines) + "\n"
    return "\n".join(head) + ("\n" if head else "") + junos_brace(stmts, inactive)


# ---------------------------------------------------------------------------------------------- main
def write(path: Path, text: str, d: Dev | None):
    data = text.replace("\n", "\r\n") if d and "crlf" in d.quirks else text
    if d and "latin1" in d.quirks:
        path.write_bytes(data.replace("description", "description Caf\xe9 Zürich", 1).encode("latin-1", "replace"))
    else:
        path.write_text(data, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    out = ROOT / "snapshot" / "configs"
    if ROOT.exists():
        shutil.rmtree(ROOT)
    out.mkdir(parents=True)
    junk = 4
    devs = build(a.count - junk, rng)
    manifest = []
    texts = {}
    for d in devs:
        r = random.Random(f"{a.seed}-{d.name}")       # rendering randomness independent of build order
        text = {"ios": render_ios, "nxos": render_nxos, "junos": render_junos}[d.family](d, r)
        texts[d.name] = text
        write(out / f"{d.name}.cfg", text, d)
        manifest.append({"file": f"{d.name}.cfg", "hostname": d.name, "site": d.site, "role": d.role, "family": d.family,
                         "model": d.model, "version": d.version, "format": d.fmt if d.family == "junos" else "native",
                         "lines": text.count("\n"), "quirks": sorted(d.quirks), "faults": d.faults})
    # junk files, the kind a config backup folder collects
    big = max((d for d in devs if d.family == "ios"), key=lambda d: texts[d.name].count("\n"))
    cut = texts[big.name][: len(texts[big.name]) // 2].replace(f"hostname {big.name}", f"hostname {big.name}-truncated")
    (out / f"{big.name}-truncated.cfg").write_text(cut)
    dup = next(d for d in devs if d.family == "ios" and "no_hostname" not in d.quirks and d.role == "campus access")
    (out / f"{dup.name}-old.cfg").write_text(texts[dup.name].replace("description", "description OLD", 1))
    (out / "empty.cfg").write_text("")
    (out / "README.txt").write_text("Config backups pulled by the nightly job.\nDo not edit by hand.\n")
    manifest += [{"file": f"{big.name}-truncated.cfg", "role": "junk", "quirks": ["truncated"]},
                 {"file": f"{dup.name}-old.cfg", "role": "junk", "quirks": ["duplicate_hostname"]},
                 {"file": "empty.cfg", "role": "junk", "quirks": ["empty"]},
                 {"file": "README.txt", "role": "junk", "quirks": ["not_a_config"]}]
    (ROOT / "manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
    fam = {}
    for d in devs:
        fam[d.family] = fam.get(d.family, 0) + 1
    print(f"wrote {len(manifest)} files to {out} ({fam}, {junk} junk); "
          f"{sum(m.get('lines', 0) for m in manifest):,} lines; manifest {ROOT / 'manifest.json'}")


if __name__ == "__main__":
    main()
