"""Generate the 50-device Cisco + Juniper test bed (deterministic, no dependencies).

    python testbed/generate.py            # writes testbed/snapshot/configs/*.cfg

Sites (all addresses are RFC 1918 / documentation ranges):
  A  NX-OS VXLAN EVPN data centre  - 2 spines, 8 leaves (4 vPC pairs), 2 border leaves      (12, NX-OS)
  B  IOS-XE campus and WAN edge     - 2 WAN routers, 2 cores, 10 access switches            (14, IOS-XE)
  C  Junos campus                   - 2 MX, 2 QFX spines, 4 QFX leaves, 16 EX access        (24, Junos)

Routing: eBGP underlay in site A (spines AS 65100, one AS per leaf) with an EVPN overlay on loopbacks;
iBGP over static-routed loopbacks between WAN routers, cores and MX pairs; eBGP everywhere else.

Deliberate faults, so every status column has something to show:
  F1 leaf-a08: overlay peer to spine-a2 with the wrong remote-as            -> HALF_OPEN both ends
  F2 mx-c2:    peer to an address nobody owns                              -> UNKNOWN_REMOTE
  F3 core-b2:  iBGP to wan-b1 loopback without update-source               -> LOCAL_IP_UNKNOWN_STATICALLY
  F4 wan-b1:   'bgp listen range' for site C transit, matched by mx-c1      -> DYNAMIC_MATCH
  F5 acc-b10:  both uplinks shut, mgmt SVI goes down on autostate
  F6 ex-c16:   ae0 member disabled, one access port disabled
  F7 leaf-a07: Vlan 1300 SVI with no member ports                           -> autostate failure
  F8 bl-a2:    eBGP to wan-b2 with ebgp-multihop but peering on connected  -> EBGP_MULTIHOP type
  F9 qfx-c6:   peer group with no peer-as on one neighbor                   -> NO_REMOTE_AS
"""
from __future__ import annotations

import ipaddress
from pathlib import Path

OUT = Path(__file__).parent / "snapshot" / "configs"

COMMON_SERVERS = {"ntp": ["10.255.1.1", "10.255.1.2"], "log": ["10.255.2.1"], "snmp": ["10.255.3.1"],
                  "tacacs": ["10.255.4.1", "10.255.4.2"], "dns": ["10.255.5.1", "10.255.5.2"]}
DOMAIN = "lab.example.net"


class P2P:
    """Allocates /31s from a pool."""

    def __init__(self, pool: str):
        self.it = ipaddress.IPv4Network(pool).subnets(new_prefix=31)

    def next(self) -> tuple[str, str]:
        n = next(self.it)
        return str(n[0]), str(n[1])


# ------------------------------------------------------------------------------------------ model
class Dev:
    def __init__(self, name: str, os: str, asn: int | None = None, lo: str | None = None):
        self.name, self.os, self.asn, self.lo = name, os, asn, lo
        self.links: list[dict] = []          # routed p2p links
        self.bgp: list[dict] = []            # neighbors
        self.extra: dict = {}

    def link(self, ifname: str, ip: str, peer: "Dev", peer_if: str, peer_ip: str, desc: str = "", vrf=None):
        self.links.append({"if": ifname, "ip": ip, "peer": peer.name, "peer_if": peer_if, "peer_ip": peer_ip,
                           "desc": desc or f"to {peer.name} {peer_if}", "vrf": vrf})


def connect(a: Dev, a_if: str, b: Dev, b_if: str, pool: P2P, vrf=None):
    ip_a, ip_b = pool.next()
    a.link(a_if, ip_a, b, b_if, ip_b, vrf=vrf)
    b.link(b_if, ip_b, a, a_if, ip_a, vrf=vrf)
    return ip_a, ip_b


def ebgp(a: Dev, a_ip: str, b: Dev, b_ip: str, a_opts: dict | None = None, b_opts: dict | None = None):
    a.bgp.append({"ip": b_ip, "asn": b.asn, "desc": b.name, **(a_opts or {})})
    b.bgp.append({"ip": a_ip, "asn": a.asn, "desc": a.name, **(b_opts or {})})


# ------------------------------------------------------------------------------------------ build topology
def build() -> list[Dev]:
    devs: list[Dev] = []
    # ---- site A
    spines = [Dev(f"spine-a{i}", "nxos", 65100, f"10.10.0.{i}") for i in (1, 2)]
    leaves = [Dev(f"leaf-a{i:02d}", "nxos", 65100 + 10 + i, f"10.10.0.{10 + i}") for i in range(1, 9)]
    borders = [Dev(f"bl-a{i}", "nxos", 65150 + i, f"10.10.0.{20 + i}") for i in (1, 2)]
    pool_a = P2P("10.10.100.0/24")
    for li, leaf in enumerate(leaves + borders, start=1):
        for si, sp in enumerate(spines, start=1):
            a, b = connect(sp, f"Ethernet1/{li}", leaf, f"Ethernet1/{48 + si}", pool_a)
            ebgp(sp, a, leaf, b)
    # EVPN overlay: every leaf to both spines on loopbacks
    for leaf in leaves + borders:
        for sp in spines:
            wrong = leaf.name == "leaf-a08" and sp.name == "spine-a2"           # F1
            leaf.bgp.append({"ip": sp.lo, "asn": 65999 if wrong else sp.asn, "desc": f"{sp.name} overlay",
                             "overlay": True})
            sp.bgp.append({"ip": leaf.lo, "asn": leaf.asn, "desc": f"{leaf.name} overlay", "overlay": True})
    for i in range(0, 8, 2):                                                    # vPC pairs
        leaves[i].extra["vpc_peer"] = leaves[i + 1]
        leaves[i + 1].extra["vpc_peer"] = leaves[i]
        leaves[i].extra["vpc_domain"] = leaves[i + 1].extra["vpc_domain"] = 10 + i // 2
    devs += spines + leaves + borders

    # ---- site B
    wans = [Dev(f"wan-b{i}", "ios", 65200, f"10.20.0.{i}") for i in (1, 2)]
    cores = [Dev(f"core-b{i}", "ios", 65210, f"10.20.0.{10 + i}") for i in (1, 2)]
    access = [Dev(f"acc-b{i:02d}", "ios", None, None) for i in range(1, 11)]
    pool_b = P2P("10.20.100.0/24")
    for wi, w in enumerate(wans, start=1):
        for ci, c in enumerate(cores, start=1):
            a, b = connect(w, f"GigabitEthernet0/0/{ci}", c, f"TenGigabitEthernet1/1/{wi}", pool_b)
            ebgp(w, a, c, b)
    a, b = connect(wans[0], "GigabitEthernet0/0/0", wans[1], "GigabitEthernet0/0/0", pool_b)
    wans[0].extra["static"] = [(wans[1].lo + "/32", b)]
    wans[1].extra["static"] = [(wans[0].lo + "/32", a)]
    wans[0].bgp.append({"ip": wans[1].lo, "asn": 65200, "desc": "wan-b2 ibgp", "upd": "Loopback0"})
    wans[1].bgp.append({"ip": wans[0].lo, "asn": 65200, "desc": "wan-b1 ibgp", "upd": "Loopback0"})
    a, b = connect(cores[0], "TenGigabitEthernet1/1/8", cores[1], "TenGigabitEthernet1/1/8", pool_b)
    cores[0].extra["static"] = [(cores[1].lo + "/32", b)]
    cores[1].extra["static"] = [(cores[0].lo + "/32", a), ("10.20.0.1/32", "10.20.100.6")]
    cores[0].bgp.append({"ip": cores[1].lo, "asn": 65210, "desc": "core-b2 ibgp", "upd": "Loopback0"})
    cores[1].bgp.append({"ip": cores[0].lo, "asn": 65210, "desc": "core-b1 ibgp", "upd": "Loopback0"})
    cores[1].bgp.append({"ip": wans[0].lo, "asn": 65200, "desc": "wan-b1 lo multihop", "multihop": True})  # F3
    for i, acc in enumerate(access, start=1):
        acc.extra["idx"] = i
    devs += wans + cores + access

    # ---- site C
    mxs = [Dev(f"mx-c{i}", "junos", 65300, f"10.30.0.{i}") for i in (1, 2)]
    qspines = [Dev(f"qfx-c{i}", "junos", 65310, f"10.30.0.{10 + i}") for i in (1, 2)]
    qleaves = [Dev(f"qfx-c{i}", "junos", 65320 + i, f"10.30.0.{10 + i}") for i in range(3, 7)]
    exs = [Dev(f"ex-c{i:02d}", "junos", None, None) for i in range(1, 17)]
    pool_c = P2P("10.30.100.0/24")
    for mi, m in enumerate(mxs):
        for qi, q in enumerate(qspines):
            a, b = connect(m, f"xe-0/0/{qi}", q, f"et-0/0/{48 + mi}", pool_c)
            ebgp(m, a, q, b)
    a, b = connect(mxs[0], "xe-0/0/9", mxs[1], "xe-0/0/9", pool_c)
    mxs[0].extra["static"] = [(mxs[1].lo + "/32", b)]
    mxs[1].extra["static"] = [(mxs[0].lo + "/32", a)]
    mxs[0].bgp.append({"ip": mxs[1].lo, "asn": 65300, "desc": "mx-c2 ibgp", "upd": "lo0", "group": "IBGP"})
    mxs[1].bgp.append({"ip": mxs[0].lo, "asn": 65300, "desc": "mx-c1 ibgp", "upd": "lo0", "group": "IBGP"})
    mxs[1].bgp.append({"ip": "192.0.2.77", "asn": 64999, "desc": "decommissioned peer", "group": "TRANSIT"})  # F2
    pool_c2 = P2P("10.30.101.0/24")
    for li, lf in enumerate(qleaves):
        for si, sp in enumerate(qspines):
            a, b = connect(sp, f"et-0/0/{li}", lf, f"et-0/0/{48 + si}", pool_c2)
            ebgp(sp, a, lf, b)
    qleaves[3].bgp.append({"ip": "10.30.101.99", "asn": None, "desc": "orphan peer", "group": "ORPHAN"})  # F9
    for i, ex in enumerate(exs):
        ex.extra["idx"] = i + 1
        ex.extra["leaf"] = qleaves[i // 4]
    devs += mxs + qspines + qleaves + exs

    # ---- WAN interconnects (cross-vendor)
    pool_w = P2P("172.16.1.0/24")
    for bi, bl in enumerate(borders):
        w = wans[bi]
        a, b = connect(w, "GigabitEthernet0/0/3", bl, "Ethernet1/47", pool_w)
        if bl.name == "bl-a2":                                                  # F8
            ebgp(w, a, bl, b, b_opts={"multihop": True})
        else:
            ebgp(w, a, bl, b)
    pool_w2 = P2P("172.16.2.0/24")
    for mi, m in enumerate(mxs):
        w = wans[mi]
        a, b = connect(w, "GigabitEthernet0/0/4", m, "xe-0/0/8", pool_w2)
        if m.name == "mx-c1":                                                   # F4: wan-b1 listens
            w.extra["listen"] = ("172.16.2.0/30", 65300)
            m.bgp.append({"ip": a, "asn": 65200, "desc": w.name, "group": "WAN"})
        else:
            ebgp(w, a, m, b, b_opts={"group": "WAN"})
    return devs


# ------------------------------------------------------------------------------------------ renderers
def mgmt_lines_ios(ip: str) -> list[str]:
    s = COMMON_SERVERS
    out = [f"ip domain name {DOMAIN}"] + [f"ip name-server {x}" for x in s["dns"]]
    out += [f"ntp server {x}" for x in s["ntp"]] + [f"logging host {x}" for x in s["log"]]
    out += [f"snmp-server host {x} version 2c public" for x in s["snmp"]]
    for i, x in enumerate(s["tacacs"], 1):
        out += [f"tacacs server TAC{i}", f" address ipv4 {x}", " key 7 0822455D0A16"]
    return out


def render_nxos(d: Dev) -> str:
    s = COMMON_SERVERS
    L = ["!Command: show running-config", "!Running configuration last done at: Thu Nov 19 09:00:00 2026",
         "version 10.3(4a) Bios:version 05.51", f"hostname {d.name}", "",
         "feature bgp", "feature interface-vlan", "feature lacp", "feature vpc", "feature nv overlay",
         "nv overlay evpn", "feature tacacs+", "", "boot nxos bootflash:/nxos64-cs.10.3.4a.M.bin", "",
         f"ip domain-name {DOMAIN}", f"ip name-server {' '.join(s['dns'])} use-vrf management"]
    L += [f"ntp server {x} use-vrf management" for x in s["ntp"]]
    L += [f"logging server {x} 6 use-vrf management" for x in s["log"]]
    L += [f"snmp-server host {x} traps version 2c public" for x in s["snmp"]]
    L += [f"tacacs-server host {x} key 7 \"fewhg123\"" for x in s["tacacs"]]
    L += ["", "ip prefix-list PL-LOOPBACKS seq 5 permit 10.10.0.0/24 le 32",
          "ip prefix-list PL-DEFAULT seq 5 permit 0.0.0.0/0", "",
          "route-map RM-UNDERLAY-OUT permit 10", "  match ip address prefix-list PL-LOOPBACKS",
          "route-map RM-UNDERLAY-OUT permit 20", "  match ip address prefix-list PL-DEFAULT", "",
          "ip access-list ACL-MGMT", "  10 permit tcp 10.255.0.0/16 any eq 22", "  20 deny ip any any", ""]
    leaf = d.name.startswith(("leaf", "bl"))
    vlans = []
    if leaf:
        idx = int(d.lo.split(".")[-1])
        vlans = [(1000 + idx, f"WEB-{idx}", 11000 + idx), (1100 + idx, f"APP-{idx}", 11100 + idx), (3001, "L3VNI", 50001)]
        if d.name == "leaf-a07":
            vlans.append((1300, "EMPTY", None))                                 # F7
        L.append("vlan 1," + ",".join(str(v[0]) for v in vlans))
        for vid, name, vni in vlans:
            L += [f"vlan {vid}", f"  name {name}"] + ([f"  vn-segment {vni}"] if vni else [])
        L += ["", "vrf context TENANT1", "  vni 50001", "  rd auto", ""]
    L += ["vrf context management", "  ip route 0.0.0.0/0 10.255.0.254", ""]
    if "vpc_peer" in d.extra:
        L += [f"vpc domain {d.extra['vpc_domain']}", "  peer-switch",
              f"  peer-keepalive destination 10.255.10.{int(d.extra['vpc_peer'].lo.split('.')[-1])} source "
              f"10.255.10.{idx} vrf management", ""]
    # interfaces
    if leaf:
        for vid, name, vni in vlans:
            if vid == 3001:
                L += [f"interface Vlan{vid}", "  no shutdown", "  vrf member TENANT1", "  ip forward", ""]
            else:
                base = (vid % 1000) if vid < 1300 else 99
                net = f"172.{16 + (vid // 100) % 10}.{vid % 100}.1/24" if vid != 1300 else "172.31.99.1/24"
                L += [f"interface Vlan{vid}", "  no shutdown", "  vrf member TENANT1" if vid != 1300 else "  description empty",
                      f"  ip address {net}", "  fabric forwarding mode anycast-gateway", ""]
        if "vpc_peer" in d.extra:
            L += ["interface port-channel1", "  description vPC peer-link", "  switchport mode trunk",
                  "  spanning-tree port type network", "  vpc peer-link", "",
                  "interface port-channel10", "  description server vPC", "  switchport mode trunk",
                  f"  switchport trunk allowed vlan {vlans[0][0]},{vlans[1][0]}", "  vpc 10", ""]
        L += ["interface nve1", "  no shutdown", "  host-reachability protocol bgp", "  source-interface loopback1"]
        for vid, name, vni in vlans:
            if vni and vid != 3001:
                L += [f"  member vni {vni}", "    ingress-replication protocol bgp"]
        L += ["  member vni 50001 associate-vrf", ""]
    used = {l["if"] for l in d.links}
    for l in d.links:
        L += [f"interface {l['if']}", f"  description {l['desc']}", "  no switchport", "  mtu 9216",
              f"  ip address {l['ip']}/31", "  no shutdown", ""]
    if leaf:
        if "vpc_peer" in d.extra:
            L += ["interface Ethernet1/41", "  description vPC peer-link member", "  switchport mode trunk",
                  "  channel-group 1 mode active", "  no shutdown", "",
                  "interface Ethernet1/42", "  description vPC peer-link member", "  switchport mode trunk",
                  "  channel-group 1 mode active", "  no shutdown", "",
                  "interface Ethernet1/1", "  description server1 nic1", "  switchport mode trunk",
                  f"  switchport trunk allowed vlan {vlans[0][0]},{vlans[1][0]}", "  channel-group 10 mode active",
                  "  no shutdown", ""]
        L += ["interface Ethernet1/2", "  description server2", "  switchport", f"  switchport access vlan {vlans[0][0]}",
              "  spanning-tree port type edge", "  no shutdown", "",
              "interface Ethernet1/3", "  description spare", "  switchport", "  shutdown", ""]
    L += ["interface mgmt0", "  vrf member management", f"  ip address 10.255.10.{int(d.lo.split('.')[-1])}/24", "",
          "interface loopback0", "  description router-id", f"  ip address {d.lo}/32", ""]
    if leaf:
        L += ["interface loopback1", "  description VTEP", f"  ip address 10.10.1.{d.lo.split('.')[-1]}/32", ""]
    # BGP
    L += [f"router bgp {d.asn}", f"  router-id {d.lo}", "  bestpath as-path multipath-relax",
          "  address-family ipv4 unicast", f"    network {d.lo}/32", "    maximum-paths 4"]
    if leaf:
        L.append(f"    network 10.10.1.{d.lo.split('.')[-1]}/32")
    L += ["  address-family l2vpn evpn", "    retain route-target all" if not leaf else "    advertise-pip"]
    for n in d.bgp:
        L += [f"  neighbor {n['ip']}", f"    remote-as {n['asn']}", f"    description {n['desc']}"]
        if n.get("overlay"):
            L += ["    update-source loopback0", "    ebgp-multihop 5", "    address-family l2vpn evpn",
                  "      send-community", "      send-community extended", "      route-map RM-UNDERLAY-OUT out"
                  if False else "      rewrite-evpn-rt-asn"]
        else:
            if n.get("multihop"):
                L.append("    ebgp-multihop 2")
            L += ["    address-family ipv4 unicast", "      send-community"]
            if d.name.startswith("spine") or d.name.startswith("bl"):
                L.append("      route-map RM-UNDERLAY-OUT out")
    if leaf:
        L += ["  vrf TENANT1", "    address-family ipv4 unicast", "      advertise l2vpn evpn"]
    return "\n".join(L) + "\n"


def render_ios(d: Dev) -> str:
    L = ["!", "! Last configuration change at 09:00:00 AEDT Thu Nov 19 2026", "!", "version 17.9",
         "service timestamps debug datetime msec", "service timestamps log datetime msec", "!",
         f"hostname {d.name}", "!", "vrf definition Mgmt-vrf", " address-family ipv4", " exit-address-family", "!",
         "banner motd ^C", "  Authorised access only - lab test bed", "^C", "!"]
    L += mgmt_lines_ios(d.name) + ["!"]
    L += ["ip access-list standard ACL-SNMP", " 10 permit 10.255.0.0 0.0.255.255", "!",
          "ip access-list extended ACL-VTY", " 10 permit tcp 10.255.0.0 0.0.255.255 any eq 22", " 20 deny ip any any", "!",
          "ip prefix-list PL-SITE-B seq 5 permit 10.20.0.0/16 le 32", "ip prefix-list PL-ANY seq 5 permit 0.0.0.0/0 le 32", "!",
          "route-map RM-OUT permit 10", " match ip address prefix-list PL-ANY", "!",
          "route-map RM-IN permit 10", " match ip address prefix-list PL-ANY", "!"]
    if d.name.startswith("acc"):
        i = d.extra["idx"]
        shut = d.name == "acc-b10"                                              # F5
        L += ["vlan 20", " name MGMT", "!", f"vlan {200 + i}", f" name USERS-{i}", "!", "vlan 999", " name PARKING", "!",
              "spanning-tree mode rapid-pvst", "spanning-tree portfast default", "!"]
        for up, core in (("TenGigabitEthernet1/1/1", "core-b1"), ("TenGigabitEthernet1/1/2", "core-b2")):
            L += [f"interface {up}", f" description uplink to {core}", " switchport trunk allowed vlan 20,{}".format(200 + i),
                  " switchport mode trunk"] + ([" shutdown"] if shut else []) + ["!"]
        for p in range(1, 9):
            L += [f"interface GigabitEthernet1/0/{p}", f" description user port {p}",
                  f" switchport access vlan {200 + i if p < 7 else 999}", " switchport mode access"]
            if p == 8:
                L.append(" shutdown")
            L.append("!")
        L += ["interface GigabitEthernet0/0", " vrf forwarding Mgmt-vrf", f" ip address 10.255.20.{i} 255.255.255.0", "!",
              "interface Vlan1", " no ip address", " shutdown", "!",
              "interface Vlan20", " description management", f" ip address 10.20.20.{10 + i} 255.255.255.0", "!",
              "ip route 0.0.0.0 0.0.0.0 10.20.20.1", "!", "line vty 0 4", " access-class ACL-VTY in", "!", "end"]
        return "\n".join(L) + "\n"
    # routers / cores
    L += [f"interface Loopback0", " description router-id", f" ip address {d.lo} 255.255.255.255", "!"]
    for l in d.links:
        net = ipaddress.IPv4Interface(l["ip"] + "/31")
        L += [f"interface {l['if']}", f" description {l['desc']}", f" ip address {l['ip']} 255.255.255.254",
              " no ip proxy-arp", "!"]
    if d.name.startswith("core"):
        ci = int(d.name[-1])
        L += ["vlan 20", " name MGMT", "!"] + [x for i in range(1, 11) for x in (f"vlan {200 + i}", f" name USERS-{i}", "!")]
        L += ["interface Port-channel1", " description core interlink L2", " switchport trunk allowed vlan 20,201-210",
              " switchport mode trunk", "!"]
        for m in ("TenGigabitEthernet1/1/5", "TenGigabitEthernet1/1/6"):
            L += [f"interface {m}", " description core interlink member", " switchport trunk allowed vlan 20,201-210",
                  " switchport mode trunk", " channel-group 1 mode active", "!"]
        for a in range(1, 11):
            L += [f"interface GigabitEthernet1/0/{a}", f" description to acc-b{a:02d}",
                  " switchport trunk allowed vlan 20,{}".format(200 + a), " switchport mode trunk", "!"]
        L += ["interface Vlan20", " description campus management", f" ip address 10.20.20.{ci + 1} 255.255.255.0",
              " standby version 2", " standby 20 ip 10.20.20.1", f" standby 20 priority {110 - ci}", "!"]
        for a in range(1, 11):
            L += [f"interface Vlan{200 + a}", f" description users acc-b{a:02d}",
                  f" ip address 10.21.{a}.{ci + 1} 255.255.255.0", f" ip helper-address 10.255.6.1",
                  f" standby {a} ip 10.21.{a}.1", "!"]
    L += ["interface GigabitEthernet0", " vrf forwarding Mgmt-vrf",
          f" ip address 10.255.20.{100 + int(d.lo.split('.')[-1])} 255.255.255.0", "!"]
    for net, nh in d.extra.get("static", []):
        n = ipaddress.IPv4Network(net)
        L.append(f"ip route {n.network_address} {n.netmask} {nh}")
    L.append("ip route 10.99.0.0 255.255.0.0 Null0")
    L += ["!", f"router bgp {d.asn}", f" bgp router-id {d.lo}", " bgp log-neighbor-changes",
          " bgp bestpath as-path multipath-relax", " no bgp default ipv4-unicast"]
    if "listen" in d.extra:
        pfx, asn = d.extra["listen"]
        L += [" neighbor SITE-C peer-group", f" neighbor SITE-C remote-as {asn}", " neighbor SITE-C description site C dynamic",
              f" bgp listen range {pfx} peer-group SITE-C"]
    for n in d.bgp:
        L.append(f" neighbor {n['ip']} remote-as {n['asn']}")
        L.append(f" neighbor {n['ip']} description {n['desc']}")
        if n.get("upd"):
            L.append(f" neighbor {n['ip']} update-source {n['upd']}")
        if n.get("multihop"):
            L.append(f" neighbor {n['ip']} ebgp-multihop 2")
    L += [" !", " address-family ipv4", f"  network {d.lo} mask 255.255.255.255"]
    if d.name.startswith("core"):
        L += ["  network 10.21.0.0 mask 255.255.0.0", "  network 10.20.20.0 mask 255.255.255.0"]
    if "listen" in d.extra:
        L += ["  neighbor SITE-C activate", "  neighbor SITE-C route-map RM-IN in"]
    for n in d.bgp:
        L += [f"  neighbor {n['ip']} activate", f"  neighbor {n['ip']} send-community both"]
        if n["asn"] != d.asn:
            L += [f"  neighbor {n['ip']} route-map RM-IN in", f"  neighbor {n['ip']} route-map RM-OUT out"]
        else:
            L.append(f"  neighbor {n['ip']} next-hop-self")
    L += ["  maximum-paths 2", " exit-address-family", "!"]
    if d.name.startswith("core"):
        L.append("ip route 10.21.0.0 255.255.0.0 Null0")
    L += ["!", "line vty 0 4", " access-class ACL-VTY in", "!", "end"]
    return "\n".join(L) + "\n"


def junos_mgmt(d: Dev, fmt: str) -> list[str]:
    s = COMMON_SERVERS
    st = [f"set system host-name {d.name}", f"set system domain-name {DOMAIN}"]
    st += [f"set system name-server {x}" for x in s["dns"]]
    st += [f"set system ntp server {x}" for x in s["ntp"]]
    st += [f"set system syslog host {x} any notice" for x in s["log"]]
    st += [f"set system tacplus-server {x} secret \"$9$abc\"" for x in s["tacacs"]]
    st += [f"set snmp trap-group TRAPS targets {x}" for x in s["snmp"]]
    st += ["set system login class NOC permissions view"]
    return st


def render_junos(d: Dev) -> list[str]:
    st = ["set version 22.4R3.25"] + junos_mgmt(d, "set")
    st += ["set groups JUMBO interfaces <*-0/0/*> mtu 9192", "set apply-groups JUMBO"]
    idx = d.extra.get("idx")
    st += ["set firewall family inet filter PROTECT-RE term SSH from source-address 10.255.0.0/16",
           "set firewall family inet filter PROTECT-RE term SSH then accept",
           "set firewall family inet filter PROTECT-RE term REST then discard"]
    mgmt_last = 30 + (idx or int(d.lo.split(".")[-1]))
    mgmt_if = "fxp0" if d.name.startswith("mx") else "em0"
    st += [f"set interfaces {mgmt_if} unit 0 family inet address 10.255.30.{mgmt_last}/24"]
    if d.name.startswith("ex"):
        i, leaf = idx, d.extra["leaf"]
        vid = 300 + i
        st += [f"set vlans USERS-{i} vlan-id {vid}", "set vlans VOICE vlan-id 400", "set vlans MGMT vlan-id 30",
               "set vlans MGMT l3-interface irb.30",
               f"set interfaces irb unit 30 family inet address 10.31.30.{10 + i}/24",
               "set chassis aggregated-devices ethernet device-count 2",
               "set interfaces ae0 description \"uplink to " + leaf.name + "\"",
               "set interfaces ae0 aggregated-ether-options lacp active",
               "set interfaces ae0 unit 0 family ethernet-switching interface-mode trunk",
               f"set interfaces ae0 unit 0 family ethernet-switching vlan members [ USERS-{i} VOICE MGMT ]",
               "set interfaces ge-0/1/0 ether-options 802.3ad ae0", "set interfaces ge-0/1/1 ether-options 802.3ad ae0"]
        if d.name == "ex-c16":                                                  # F6
            st += ["set interfaces ge-0/1/1 disable", "set interfaces ge-0/0/5 disable"]
        for p in range(0, 6):
            st += [f"set interfaces ge-0/0/{p} description \"user port {p}\"",
                   f"set interfaces ge-0/0/{p} unit 0 family ethernet-switching interface-mode access",
                   f"set interfaces ge-0/0/{p} unit 0 family ethernet-switching vlan members USERS-{i}"]
        st += ["set routing-options static route 0.0.0.0/0 next-hop 10.31.30.1",
               "set protocols lldp interface all", "set protocols rstp interface ae0"]
        return st
    st += [f"set interfaces lo0 unit 0 family inet address {d.lo}/32",
           "set interfaces lo0 unit 0 family inet filter input PROTECT-RE"]
    for l in d.links:
        st += [f"set interfaces {l['if']} description \"{l['desc']}\"",
               f"set interfaces {l['if']} unit 0 family inet address {l['ip']}/31"]
    if d.name in ("qfx-c3", "qfx-c4", "qfx-c5", "qfx-c6"):
        li = int(d.name[-1]) - 2
        st += ["set chassis aggregated-devices ethernet device-count 8", "set vlans MGMT vlan-id 30",
               "set vlans MGMT l3-interface irb.30", "set vlans VOICE vlan-id 400"]
        if li in (1, 2):
            st += [f"set interfaces irb unit 30 family inet address 10.31.30.{li}/24"]
        for k in range(4):
            ex = 4 * (li - 1) + k + 1
            st += [f"set vlans USERS-{ex} vlan-id {300 + ex}", f"set vlans USERS-{ex} l3-interface irb.{300 + ex}",
                   f"set vlans USERS-{ex} vxlan vni {30000 + ex}",
                   f"set interfaces irb unit {300 + ex} family inet address 10.32.{ex}.1/24",
                   f"set interfaces ae{k} description \"to ex-c{ex:02d}\"",
                   f"set interfaces ae{k} aggregated-ether-options lacp active",
                   f"set interfaces ae{k} unit 0 family ethernet-switching interface-mode trunk",
                   f"set interfaces ae{k} unit 0 family ethernet-switching vlan members [ USERS-{ex} VOICE MGMT ]",
                   f"set interfaces xe-0/0/{10 + 2 * k} ether-options 802.3ad ae{k}",
                   f"set interfaces xe-0/0/{11 + 2 * k} ether-options 802.3ad ae{k}"]
    for net, nh in d.extra.get("static", []):
        st.append(f"set routing-options static route {net} next-hop {nh}")
    st += [f"set routing-options router-id {d.lo}", f"set routing-options autonomous-system {d.asn}",
           "set routing-options static route 10.99.0.0/16 discard",
           "set policy-options prefix-list PL-LOOPBACKS 10.30.0.0/24",
           "set policy-options policy-statement EXPORT-DIRECT term LO from protocol direct",
           "set policy-options policy-statement EXPORT-DIRECT term LO from route-filter 10.30.0.0/16 orlonger",
           "set policy-options policy-statement EXPORT-DIRECT term LO then accept",
           "set policy-options policy-statement EXPORT-DIRECT term IRB from protocol direct",
           "set policy-options policy-statement EXPORT-DIRECT term IRB from route-filter 10.32.0.0/16 orlonger",
           "set policy-options policy-statement EXPORT-DIRECT term IRB then accept",
           "set policy-options policy-statement EXPORT-DIRECT term BGP from protocol bgp",
           "set policy-options policy-statement EXPORT-DIRECT term BGP then accept",
           "set policy-options policy-statement EXPORT-DIRECT then reject",
           "set policy-options policy-statement ACCEPT-ALL then accept",
           "set protocols bgp group EBGP type external", "set protocols bgp group EBGP multipath multiple-as",
           "set protocols bgp group EBGP export EXPORT-DIRECT", "set protocols bgp group EBGP import ACCEPT-ALL"]
    for n in d.bgp:
        grp = n.get("group", "EBGP")
        if grp == "IBGP":
            st += ["set protocols bgp group IBGP type internal", f"set protocols bgp group IBGP local-address {d.lo}",
                   "set protocols bgp group IBGP export EXPORT-DIRECT",
                   f"set protocols bgp group IBGP neighbor {n['ip']} description \"{n['desc']}\""]
            continue
        if grp != "EBGP":
            st += [f"set protocols bgp group {grp} type external", f"set protocols bgp group {grp} export EXPORT-DIRECT"]
        st.append(f"set protocols bgp group {grp} neighbor {n['ip']} description \"{n['desc']}\"")
        if n["asn"] is not None:
            st.append(f"set protocols bgp group {grp} neighbor {n['ip']} peer-as {n['asn']}")
        local = next((l["ip"] for l in d.links if l["peer_ip"] == n["ip"]), None)
        if local:
            st.append(f"set protocols bgp group {grp} neighbor {n['ip']} local-address {local}")
    return st


def to_hierarchical(stmts: list[str]) -> str:
    """Render set statements as a Junos curly-brace config."""
    tree: dict = {}
    import shlex
    for s in stmts:
        toks = shlex.split(s[4:], posix=False)
        if "[" in toks:
            a = toks.index("[")
            b = toks.index("]")
            toks = toks[:a] + ["[ " + " ".join(toks[a + 1:b]) + " ]"] + toks[b + 1:]
        node = tree
        # leaf statements: last two tokens form "key value" except single keywords
        for t in toks[:-1]:
            node = node.setdefault(t, {})
        node.setdefault(toks[-1], None)

    def emit(node: dict, depth: int) -> list[str]:
        out = []
        pad = "    " * depth
        for k, v in node.items():
            if v is None:
                out.append(f"{pad}{k};")
            elif all(x is None for x in v.values()) and len(v) == 1 and not k.startswith(("unit", "term", "group")):
                (leaf,) = v
                out.append(f"{pad}{k} {leaf};")
            else:
                out.append(f"{pad}{k} {{")
                out += emit(v, depth + 1)
                out.append(f"{pad}}}")
        return out

    return "\n".join(["## Last commit: 2026-11-19 09:00:00 AEDT by netops"] + emit(tree, 0)) + "\n"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("*"):
        f.unlink()
    devs = build()
    assert len(devs) == 50, len(devs)
    for d in devs:
        if d.os == "nxos":
            (OUT / f"{d.name}.cfg").write_text(render_nxos(d))
        elif d.os == "ios":
            (OUT / f"{d.name}.cfg").write_text(render_ios(d))
        else:
            st = render_junos(d)
            # half the Junos devices in set format, half hierarchical, to exercise both front ends
            if d.name.startswith(("mx", "qfx-c1", "qfx-c3", "qfx-c5")) or d.name in ("ex-c01", "ex-c03", "ex-c05", "ex-c07"):
                (OUT / f"{d.name}.cfg").write_text(to_hierarchical(st))
            else:
                (OUT / f"{d.name}.cfg").write_text("\n".join(st) + "\n")
    print(f"wrote {len(devs)} configs to {OUT}")


if __name__ == "__main__":
    main()
