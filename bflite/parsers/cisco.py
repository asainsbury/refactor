"""Cisco IOS / IOS-XE and NX-OS parser.

Semantics follow Batfish's CiscoControlPlaneExtractor / CiscoConfiguration (IOS) and
CiscoNxosPreprocessor / CiscoNxosConfiguration / Conversions (NX-OS).
"""
from __future__ import annotations

import ipaddress
import re
from typing import Optional

from ..ifnames import canonical, kind_of
from ..model import DEFAULT_VRF, Address, BgpPeer, BgpProcess, Interface, Node, Vlan
from ..util import format_int_space, is_ipv4, is_ipv4_prefix, parse_address, parse_asn, parse_vlan_list
from .tree import Line, build_tree

IOS, NXOS = "CISCO_IOS", "CISCO_NXOS"

# IOS default speeds (Batfish cisco/Interface.getDefaultSpeed) - prefix match on canonical name
_IOS_SPEEDS = [("Ethernet", 1e7), ("FastEthernet", 100e6), ("FiftyGig", 50e9), ("FiveGigabit", 5e9),
               ("FortyGig", 40e9), ("GigabitEthernet", 1e9), ("HundredGig", 100e9), ("LongReachEthernet", 10e6),
               ("TenGigabitEthernet", 10e9), ("TwentyFiveGigE", 25e9), ("TwoGigabitEthernet", 2.5e9),
               ("Wlan-GigabitEthernet", 1e9)]


def _ios_default_speed(name: str) -> Optional[float]:
    for p, s in _IOS_SPEEDS:
        if name.startswith(p):
            return s
    return None


def _ios_default_bandwidth(name: str) -> Optional[float]:
    s = _ios_default_speed(name)
    if s is not None:
        return s
    if name.startswith("Loopback"):
        return 8e9
    if name.startswith("Port-channel"):
        return None
    if name.startswith("Vlan"):
        return 1e9
    if name.startswith("Tunnel"):
        return 100e3
    return 1e12


def _nx_type(name: str) -> Optional[str]:
    n = name.lower()
    for prefix, t in (("ethernet", "ETHERNET"), ("port-channel", "PORT_CHANNEL"), ("loopback", "LOOPBACK"),
                      ("mgmt", "MGMT"), ("vlan", "VLAN")):
        if n.startswith(prefix):
            return t
    return None  # nve, Tunnel etc are not converted by Batfish NX-OS


def _rest(line: Line, n: int) -> str:
    parts = line.text.split(None, n)
    return parts[n] if len(parts) > n else ""


class CiscoParser:
    def __init__(self, text: str, vendor: str, filename: str = ""):
        self.text = text
        self.vendor = vendor
        self.filename = filename
        self.tree = build_tree(text)
        self.acls: set[str] = set()
        self.route_maps: set[str] = set()
        self.vrf_shutdown: set[str] = set()
        self.node: Node

    # ------------------------------------------------------------------ top level
    def parse(self) -> Node:
        host = None
        for c in self.tree.children:
            w = c.words
            if w[0] in ("hostname", "switchname") and len(w) > 1:
                host = w[1].strip('"')
        hostname = (host or self.filename.rsplit("/", 1)[-1].rsplit(".", 1)[0]).lower()
        self.node = Node(hostname=hostname, vendor=self.vendor, filename=self.filename)
        if self.vendor == NXOS:
            self.node.vrfs.add("management")
        else:
            self._ios_absorb()
        self._collect_names()
        self._parse_vlans()
        if self.vendor == NXOS:
            self._nxos_preprocess()
        self._parse_interfaces()
        if self.vendor == NXOS:
            self._nxos_tenant_vni_interfaces()
        self._parse_bgp()
        return self.node

    def _nxos_tenant_vni_interfaces(self) -> None:
        """VxlanTopologyUtils.addTenantVniInterfaces: one 'nve~<vni>' TUNNEL interface in the tenant VRF
        per layer-3 VNI, with link-local 169.254.0.1. CiscoNxosConfiguration.convertNveVni only creates
        the L3 VNI when the VNI has a VLAN, a VRF context has 'vni <vni>', an SVI for that VLAN is active
        in that VRF, and the NVE source-interface has an address."""
        vrf_vni: dict[int, str] = {}
        for c in self.tree.children:
            low = [x.lower() for x in c.words]
            if low[:2] == ["vrf", "context"] and len(c.words) > 2:
                for ch in c.children:
                    cw = [x.lower() for x in ch.words]
                    if cw[0] == "vni" and len(cw) > 1 and cw[1].isdigit():
                        vrf_vni.setdefault(int(cw[1]), c.words[2])
        for vni, src in self.nve_l3:
            vlan, vrf = self.vni_vlan_all.get(vni), vrf_vni.get(vni)
            if vlan is None or vrf is None or vrf not in self.node.vrfs:
                continue
            if not any(i.kind == "VLAN" and i.vlan_id == vlan and i.vrf == vrf and i.admin_up
                       and not i.inactive_reason_pre for i in self.node.interfaces.values()):
                continue
            s = self.node.interfaces.get(src) if src else None
            if s is None or s.primary is None:
                continue
            t = self.node.iface(f"nve~{vni}")
            t.declared_names, t.kind, t.vrf, t.link_local = [], "TUNNEL", vrf, "169.254.0.1"
            t.admin_up, t.proxy_arp, t.switchport, t.switchport_mode = True, False, False, "NONE"

    def _ios_absorb(self) -> None:
        """Batfish's IOS grammar ignores indentation ('!' is a comment, not the end of a stanza), so a
        top-level line that is also valid inside the stanza above it is parsed as part of that stanza.
        Two cases are reproduced (both seen on the test bed, see batfish.md):

        * 'vlan <id>' stanzas straight after an interface become interface 'vlan <id>' commands
          (Cisco_interface.g4 if_vlan, which sets Encapsulation_VLAN; the last one wins), and their
          children are read as interface lines. They don't define VLANs.
        * 'spanning-tree ...' lines straight after a 'vlan' stanza are swallowed by it
          (Cisco_vlan.g4 vlan_spanning_tree_null), so 'spanning-tree portfast default' there does nothing.
        """
        kept: list[Line] = []
        ctx: Optional[str] = None
        ctx_line: Optional[Line] = None
        for c in self.tree.children:
            low = [x.lower() for x in c.words]
            if ctx == "interface" and len(low) == 2 and low[0] == "vlan" and low[1].isdigit():
                ctx_line.children.append(Line(c.text, ctx_line.indent + 1))
                ctx_line.children.extend(c.children)
                continue
            if ctx == "vlan" and low[0] == "spanning-tree":
                continue
            kept.append(c)
            if low[0] == "interface":
                ctx, ctx_line = "interface", c
            elif low[0] == "vlan" and len(low) == 2 and re.match(r"^[\d,\-]+$", low[1]):
                ctx, ctx_line = "vlan", c
            else:
                ctx, ctx_line = None, None
        self.tree.children = kept

    def _collect_names(self) -> None:
        # Index of the top-level 'spanning-tree portfast ... default' line. Batfish copies the default
        # into each IOS interface when the interface is created (Interface constructor), so only
        # interfaces declared after this line get it.
        self.portfast_default_at: Optional[int] = None
        for idx, c in enumerate(self.tree.children):
            low = [x.lower() for x in c.words]
            if (self.vendor != NXOS and low[:2] == ["spanning-tree", "portfast"] and "default" in low[2:]
                    and set(low[2:]) <= {"bpdufilter", "bpduguard", "default", "edge"}
                    and self.portfast_default_at is None):
                self.portfast_default_at = idx
        for c in self.tree.children:
            w = c.words
            low = [x.lower() for x in w]
            if low[:2] == ["ip", "access-list"] and len(w) >= 3:
                name = w[3] if low[2] in ("extended", "standard") and len(w) > 3 else w[2]
                self.acls.add(name)
            elif low[0] == "access-list" and len(w) > 1:
                self.acls.add(w[1])
            elif low[0] == "route-map" and len(w) > 1:
                self.route_maps.add(w[1])
            elif low[:2] == ["ip", "vrf"] and len(w) > 2:
                self.node.vrfs.add(w[2])
            elif low[:2] == ["vrf", "definition"] and len(w) > 2:
                self.node.vrfs.add(w[2])
            elif low[:2] == ["vrf", "context"] and len(w) > 2:
                self.node.vrfs.add(w[2])
                if any(ch.text.lower() == "shutdown" for ch in c.children):
                    self.vrf_shutdown.add(w[2])

    def _parse_vlans(self) -> None:
        # NX-OS has no implicit VLAN 1: an access VLAN or trunk VLAN 1 only counts if a 'vlan' line defines it
        for c in self.tree.children:
            w = c.words
            if w[0].lower() == "vlan" and len(w) == 2 and re.match(r"^[\d,\-]+$", w[1]):
                for vid in parse_vlan_list(w[1]):
                    v = self.node.vlans.setdefault(vid, Vlan(vid))
                    for ch in c.children:
                        cw = ch.words
                        if cw[0] == "name" and len(cw) > 1:
                            v.name = cw[1]
                        elif cw[0] == "vn-segment" and len(cw) > 1:
                            v.vni = int(cw[1])
        # Layer-2 VNIs need a member of an NVE that is not shut down (Batfish only builds Layer2Vni for
        # NVE members, and Nve starts shut down: CiscoNxosConfiguration.convertNveVni).
        nve_l2: set[int] = set()
        self.nve_l3: list[tuple[int, Optional[str]]] = []      # (L3 VNI, NVE source-interface)
        for c in self.tree.children:
            if len(c.words) > 1 and c.words[0].lower() == "interface" and c.words[1].lower().startswith("nve"):
                kids = [[x.lower() for x in ch.words] for ch in c.children]
                if ["no", "shutdown"] not in kids:
                    continue
                src = next((canonical(ch.words[1], NXOS) for ch, cw in zip(c.children, kids)
                            if cw[0] == "source-interface" and len(cw) > 1), None)
                for cw in kids:
                    if cw[:2] == ["member", "vni"] and "associate-vrf" not in cw:
                        for vni in parse_vlan_list(cw[2]):
                            nve_l2.add(vni)
                    elif cw[:2] == ["member", "vni"] and cw[2].isdigit():
                        self.nve_l3.append((int(cw[2]), src))
        self.vni_vlan_all = {v.vni: v.vlan_id for v in self.node.vlans.values() if v.vni is not None}
        for v in self.node.vlans.values():
            if v.vni is not None and self.vendor == NXOS and v.vni not in nve_l2:
                v.vni = None

    # ------------------------------------------------------------------ NX-OS defaults inference
    def _nxos_preprocess(self) -> None:
        """Port of CiscoNxosPreprocessor: infer platform, L2/L3 default mode, L3 default shutdown."""
        version, images = None, []
        explicit_default_switchport = None
        explicit_switchport_shutdown = None
        for c in self.tree.children:
            low = [x.lower() for x in c.words]
            if low[0] == "version" and len(low) > 1:
                version = low[1]
            elif low[0] == "boot" and len(low) > 2 and low[1] in ("nxos", "system", "kickstart"):
                images.append(low[2])      # the image; a trailing 'sup-1' / 'sup-2' names the supervisor
            elif low[:3] == ["system", "default", "switchport"]:
                if len(low) == 3:
                    explicit_default_switchport = True
                elif low[3] == "shutdown":
                    explicit_switchport_shutdown = True
            elif low[:4] == ["no", "system", "default", "switchport"]:
                if len(low) == 4:
                    explicit_default_switchport = False
                elif low[4] == "shutdown":
                    explicit_switchport_shutdown = False

        def major_from(ver: Optional[str]) -> Optional[str]:
            if not ver:
                return None
            if ver[0] in "45679":
                return "NXOS" + ver[0]
            if ver.startswith("10"):
                return "NXOS10"
            return None

        major = major_from(version)
        if major is None:
            for img in images:
                m = re.search(r"(\d+)\.(\d+)", img.split(":")[-1])
                if m:
                    major = major_from(m.group(1) + "." + m.group(2))
                    if major:
                        break
        major = major or "UNKNOWN"

        # evidence counting
        l2_ev = l3_ev = l3_shut = l3_noshut = 0
        for c in self.tree.children:
            w = c.words
            if w[0].lower() != "interface" or len(w) < 2:
                continue
            name = canonical(" ".join(w[1:]), NXOS)
            t = _nx_type(name)
            sub = "." in name
            switchport_decided = None
            shut = None
            layer3 = False
            for ch in c.children:
                low = [x.lower() for x in ch.words]
                evidence = (explicit_default_switchport is None and not sub and switchport_decided is None
                            and t in ("ETHERNET", "PORT_CHANNEL"))
                if low == ["no", "switchport"]:
                    if evidence:
                        l2_ev += 1
                    switchport_decided = False
                    layer3 = True
                elif low == ["switchport"]:
                    if evidence:
                        l3_ev += 1
                    switchport_decided = True
                elif low[0] == "switchport":
                    if evidence:
                        l2_ev += 1
                elif low[:2] in (["ip", "address"], ["vrf", "member"]):
                    if evidence:
                        l3_ev += 1
                    layer3 = True
                elif low[0] == "shutdown" and (len(low) == 1 or low[1] == "force"):
                    shut = True
                elif low == ["no", "shutdown"]:
                    shut = False
            if shut is not None and not sub and layer3:
                if shut:
                    l3_shut += 1
                else:
                    l3_noshut += 1

        platform = "UNKNOWN"
        if images:
            img = images[0]
            if "n3000" in img:
                platform = "NEXUS_3000"
            elif "n5000" in img:
                platform = "NEXUS_5000"
            elif "n6000" in img:
                platform = "NEXUS_6000"
            elif "n7000" in img or "n7700" in img or "titanium" in img:
                platform = "NEXUS_7000"
            elif major in ("NXOS9", "NXOS10"):
                platform = "NEXUS_9000"
            elif l3_noshut == 0 and l3_shut > 0:
                platform = "NEXUS_3000"
            elif l3_noshut > 0 and l3_shut == 0:
                platform = "NEXUS_9000"
            else:
                platform = "NEXUS_9000"

        non_switchport_default_shutdown = {
            "NEXUS_3000": major in ("NXOS9", "NXOS10"), "NEXUS_9000": True,
        }.get(platform, False)
        if platform in ("NEXUS_3000", "NEXUS_5000", "NEXUS_6000"):
            default_switchport = True
        elif platform == "NEXUS_7000":
            default_switchport = False
        elif major == "NXOS5":
            default_switchport = True
        elif major == "NXOS6":
            default_switchport = False
        else:
            default_switchport = l2_ev > l3_ev
        if explicit_default_switchport is not None:
            default_switchport = explicit_default_switchport
        self.nx_platform = platform
        self.nx_default_switchport = default_switchport
        self.nx_nonswitchport_default_shutdown = non_switchport_default_shutdown
        self.nx_switchport_default_shutdown = bool(explicit_switchport_shutdown)

    # ------------------------------------------------------------------ interfaces
    def _parse_interfaces(self) -> None:
        nx = self.vendor == NXOS
        for idx, c in enumerate(self.tree.children):
            w = c.words
            if w[0].lower() != "interface" or len(w) < 2:
                continue
            self._portfast_default = self.portfast_default_at is not None and idx > self.portfast_default_at
            raw_name = " ".join(w[1:])
            if raw_name.lower().startswith("range"):
                continue
            name = canonical(raw_name, self.vendor)
            if nx and _nx_type(name) is None:
                continue
            itf = self.node.iface(name)
            # IOS: the names as written (CiscoControlPlaneExtractor.addInterface); NX-OS: as before
            itf.declared_names = [raw_name] if not nx else (sorted({raw_name, name}) if raw_name != name else [name])
            itf.kind = kind_of(name, self.vendor)
            if "." in name:
                itf.parent = name.split(".", 1)[0]
                itf.kind = "LOGICAL"
            if itf.kind == "VLAN":
                m = re.search(r"(\d+)$", name)
                itf.vlan_id = int(m.group(1)) if m else None
            self._parse_interface_body(itf, c)

    def _parse_interface_body(self, itf: Interface, block: Line) -> None:
        nx = self.vendor == NXOS
        shutdown: Optional[bool] = None
        switchport: Optional[bool] = None
        mode: Optional[str] = None
        access_vlan: Optional[int] = None
        native_vlan: Optional[int] = None
        allowed: Optional[set[int]] = None
        primary: Optional[Address] = None
        secondaries: list[Address] = []
        speed_mbps: Optional[float] = None
        bandwidth_kbps: Optional[float] = None
        proxy_arp: Optional[bool] = None
        portfast: Optional[bool] = None
        hsrp_version = None
        vrf = DEFAULT_VRF
        for ch in block.children:
            w = ch.words
            low = [x.lower() for x in w]
            if low[0] == "description":
                itf.description = _rest(ch, 1)
                if nx and not 1 <= len(itf.description) <= 254:   # INTERFACE_DESCRIPTION_LENGTH_RANGE
                    itf.description = None
            elif low[0] == "shutdown" and (len(low) == 1 or low[1] == "force"):
                shutdown = True
            elif low == ["no", "shutdown"]:
                shutdown = False
            elif low[:2] == ["ip", "address"] and len(w) > 2 and low[2] not in ("dhcp", "negotiated"):
                addr = parse_address(w[2:4] if "/" not in w[2] else w[2:3])
                if addr:
                    if "secondary" in low:
                        secondaries.append(addr)
                    else:
                        primary = addr
            elif low[:2] == ["ip", "unnumbered"] and len(w) > 2:
                itf.unnumbered = canonical(w[2], self.vendor)
            elif low[:2] == ["vrf", "forwarding"] and len(w) > 2:
                vrf = w[2]
            elif low[:3] == ["ip", "vrf", "forwarding"] and len(w) > 3:
                vrf = w[3]
            elif low[:2] == ["vrf", "member"] and len(w) > 2:
                vrf = w[2]
            elif low == ["switchport"]:
                switchport = True
                if mode in (None, "NONE"):
                    mode = None if nx else "DYNAMIC_AUTO"
            elif low == ["switchport", "monitor"] and nx:
                switchport, mode = True, "MONITOR"
            elif low == ["no", "switchport"]:
                switchport = False
                mode = "NONE"
            elif low[:2] == ["switchport", "mode"] and len(low) > 2:
                if low[2] == "monitor":
                    continue
                mode = {"access": "ACCESS", "trunk": "TRUNK", "dot1q-tunnel": "DOT1Q_TUNNEL",
                        "fex-fabric": "FEX_FABRIC", "tap": "TAP", "tool": "TOOL"}.get(low[2])
                if low[2] == "dynamic":
                    mode = "DYNAMIC_DESIRABLE" if "desirable" in low else "DYNAMIC_AUTO"
                switchport = True
            elif low[:3] == ["switchport", "access", "vlan"] and len(low) > 3 and low[3].isdigit():
                access_vlan = int(low[3])
                if not nx:
                    switchport, mode = True, "ACCESS"
                elif mode is None:     # NX-OS exitI_switchport_access: sets ACCESS when no mode is set yet
                    mode = "ACCESS"
            elif low[:4] == ["switchport", "trunk", "native", "vlan"] and len(low) > 4:
                native_vlan = int(low[4])
            elif low[:4] == ["switchport", "trunk", "allowed", "vlan"] and len(low) > 4:
                arg = low[4]
                if arg == "none":
                    allowed = set()
                elif arg == "all":
                    allowed = set(range(1, 4095))
                elif arg == "add" and len(low) > 5:
                    allowed = (allowed or (set(range(1, 4095)) if nx else set())) | parse_vlan_list(low[5])
                elif arg == "remove" and len(low) > 5:
                    allowed = (allowed if allowed is not None else set(range(1, 4095))) - parse_vlan_list(low[5])
                elif arg == "except" and len(low) > 5:
                    allowed = set(range(1, 4095)) - parse_vlan_list(low[5])
                else:
                    allowed = parse_vlan_list(arg)
            elif low[:3] == ["switchport", "trunk", "encapsulation"] and len(low) > 3:
                itf.trunk_encapsulation = {"dot1q": "DOT1Q", "isl": "ISL", "negotiate": "NEGOTIATE"}.get(low[3], "DOT1Q")
                if not nx:
                    mode = "TRUNK"
            elif low[0] == "channel-group" and len(low) > 1 and low[1].isdigit():
                itf.channel_group = ("port-channel" if nx else "Port-channel") + low[1]
            elif low[0] == "mtu" and len(low) > 1 and low[1].isdigit():
                itf.mtu = int(low[1])
            elif low[0] == "bandwidth" and len(low) > 1 and low[1].isdigit():
                bandwidth_kbps = float(low[1])
            elif low[0] == "speed" and len(low) > 1 and low[1].isdigit():
                speed_mbps = float(low[1])
            elif low[:2] == ["ip", "access-group"] and len(w) > 3:
                if w[2] in self.acls:
                    if low[3] == "in":
                        itf.in_filter = w[2]
                    elif low[3] == "out":
                        itf.out_filter = w[2]
            elif low[:3] == ["ip", "policy", "route-map"] and len(w) > 3:
                if not nx or w[3] in self.route_maps:
                    itf.pbr_policy = w[3]
            elif low[:2] == ["ip", "helper-address"] and len(w) > 2 and is_ipv4(w[-1]):
                itf.dhcp_relay.append(w[-1])
            elif low[:4] == ["ip", "dhcp", "relay", "address"] and len(w) > 4:
                itf.dhcp_relay.append(w[4])
            elif low[0] == "standby" and not nx:
                if len(low) > 2 and low[1] == "version":
                    hsrp_version = low[2]
                elif len(low) >= 2:
                    grp = low[1] if low[1].isdigit() else "0"
                    if grp not in itf.hsrp_groups:
                        itf.hsrp_groups.append(grp)
            elif low[0] == "hsrp" and nx and len(low) > 1:
                if low[1] == "version" and len(low) > 2:
                    hsrp_version = low[2]
                elif low[1].isdigit() and low[1] not in itf.hsrp_groups:
                    itf.hsrp_groups.append(low[1])
            elif low[0] == "vrrp" and len(low) > 1 and low[1].isdigit() and "address-family" not in low:
                # Batfish reads 'vrrp N ip ...' style only; IOS-XE 'vrrp N address-family ipv4' gives no group
                g = int(low[1])
                if g not in itf.vrrp_groups:
                    itf.vrrp_groups.append(g)
            elif low[:3] == ["no", "ip", "proxy-arp"]:
                proxy_arp = False
            elif low[:2] == ["ip", "proxy-arp"]:
                proxy_arp = True
            elif low[:2] == ["spanning-tree", "portfast"] and not nx:
                portfast = not (len(low) > 2 and low[2] == "disable")
            elif low[0] == "encapsulation" and len(low) > 2 and low[1] == "dot1q":
                itf.encapsulation_vlan = int(low[2])
            elif low[0] == "vlan" and len(low) == 2 and low[1].isdigit() and not nx:
                itf.encapsulation_vlan = int(low[1])     # if_vlan (see _ios_absorb)
            elif low == ["no", "autostate"]:
                itf.autostate = False
            elif low[:2] == ["zone-member", "security"] and len(w) > 2:
                itf.zone = w[2]

        itf.vrf = vrf
        itf.hsrp_version = hsrp_version
        itf.hsrp_groups.sort(key=lambda g: int(g))
        if vrf not in self.node.vrfs:
            if nx:   # NX-OS: member of undefined VRF -> stays in default VRF and is disabled (INVALID)
                itf.vrf = DEFAULT_VRF
                itf.inactive_reason_pre = "Invalid configuration"
            else:    # IOS: VRF is created implicitly
                self.node.vrfs.add(vrf)

        if nx:
            self._finish_nxos_interface(itf, shutdown, switchport, mode, access_vlan, native_vlan, allowed,
                                        primary, secondaries, speed_mbps, bandwidth_kbps, proxy_arp)
        else:
            self._finish_ios_interface(itf, shutdown, switchport, mode, access_vlan, native_vlan, allowed,
                                       primary, secondaries, speed_mbps, bandwidth_kbps, proxy_arp, portfast)

    def _finish_ios_interface(self, itf, shutdown, switchport, mode, access_vlan, native_vlan, allowed,
                              primary, secondaries, speed_mbps, bandwidth_kbps, proxy_arp, portfast):
        itf.admin_up = not bool(shutdown)
        itf.switchport = bool(switchport)
        # CiscoConfiguration.toInterface copies portfast only for switchports; the vendor value starts as
        # the global default at creation time and an interface 'spanning-tree portfast [disable]' overrides it.
        itf.portfast = itf.switchport and (portfast if portfast is not None else self._portfast_default)
        itf.proxy_arp = True if proxy_arp is None else proxy_arp
        itf.speed = speed_mbps * 1e6 if speed_mbps is not None else _ios_default_speed(itf.name)
        if bandwidth_kbps is not None:
            itf.bandwidth = bandwidth_kbps * 1000
        else:
            itf.bandwidth = itf.speed if itf.speed is not None else _ios_default_bandwidth(itf.name)
        if itf.switchport:
            itf.switchport_mode = mode or "NONE"
            if itf.switchport_mode == "ACCESS":
                itf.access_vlan = access_vlan if access_vlan is not None else 1
            if itf.switchport_mode == "TRUNK":
                itf.allowed_vlans = allowed if allowed is not None else set(range(1, 4095))
                itf.native_vlan = native_vlan if native_vlan is not None else 1   # trunks only
            itf.encapsulation_vlan = None
        else:
            itf.switchport_mode = "NONE"
            if primary:
                itf.addresses.append(primary)
            itf.addresses.extend(a for a in secondaries if a != primary)
        if itf.kind != "VLAN":
            itf.autostate = True

    def _finish_nxos_interface(self, itf, shutdown, switchport, mode, access_vlan, native_vlan, allowed,
                               primary, secondaries, speed_mbps, bandwidth_kbps, proxy_arp):
        t = _nx_type(itf.name)
        sub = itf.parent is not None
        if mode is None and switchport is True:
            mode = "ACCESS"
        if mode is None and switchport is False:
            mode = "NONE"
        if mode is None:
            if t in ("ETHERNET", "PORT_CHANNEL") and not sub:
                mode = "ACCESS" if self.nx_default_switchport else "NONE"
            else:
                mode = "NONE"
        if shutdown is None:
            if t == "ETHERNET":
                shutdown = (self.nx_switchport_default_shutdown if mode != "NONE"
                            else self.nx_nonswitchport_default_shutdown)
                if sub:
                    shutdown = True
            elif t == "PORT_CHANNEL":
                shutdown = sub
            elif t == "VLAN":
                shutdown = True
            else:
                shutdown = False
        itf.admin_up = not shutdown
        itf.proxy_arp = bool(proxy_arp)
        itf.switchport = mode != "NONE"
        itf.switchport_mode = mode
        if mode == "ACCESS":
            av = access_vlan if access_vlan is not None else 1
            itf.access_vlan = av if av in self.node.vlans else None
        elif mode == "TRUNK":
            al = allowed if allowed is not None else set(range(1, 4095))
            itf.allowed_vlans = al & set(self.node.vlans)
            itf.native_vlan = native_vlan if native_vlan is not None else 1
        if mode == "NONE":
            if primary:
                itf.addresses.append(primary)
            itf.addresses.extend(a for a in secondaries if a != primary)
        else:
            itf.encapsulation_vlan = None
        if itf.kind != "VLAN":
            itf.autostate = True
        default_speed = 1e9 if t == "ETHERNET" else None
        itf.speed = speed_mbps * 1e6 if speed_mbps is not None else default_speed
        if bandwidth_kbps is not None:
            itf.bandwidth = bandwidth_kbps * 1000
        elif itf.speed is not None:
            itf.bandwidth = itf.speed
        else:
            itf.bandwidth = {"LOOPBACK": 8e9, "MGMT": 1e9, "PORT_CHANNEL": None, "VLAN": 1e9}.get(t, 1e12)
        if itf.vrf in self.vrf_shutdown and itf.admin_up:
            itf.inactive_reason_pre = "Interface's VRF is administratively down"
        if itf.hsrp_groups and itf.hsrp_version is None:
            itf.hsrp_version = "1"

    # ------------------------------------------------------------------ BGP
    def _parse_bgp(self) -> None:
        for c in self.tree.children:
            w = c.words
            if w[0].lower() == "router" and len(w) > 2 and w[1].lower() == "bgp":
                asn = parse_asn(w[2])
                if self.vendor == NXOS:
                    self._nxos_bgp(c, asn)
                else:
                    self._ios_bgp(c, asn)

    # ---- IOS
    def _ios_bgp(self, block: Line, asn: Optional[int]) -> None:
        # per-vrf raw state
        procs: dict[str, dict] = {}

        def proc(vrf: str) -> dict:
            return procs.setdefault(vrf, {"router_id": None, "default_v4": True, "max_paths": 0, "max_ebgp": 0,
                                          "max_ibgp": 0, "relax": False, "tie": None, "cluster_id": None,
                                          "confed": None, "confed_members": None, "peers": {}, "groups": {},
                                          "order": []})

        def peer(p: dict, key: str, create: bool) -> Optional[dict]:
            if key in p["peers"]:
                return p["peers"][key]
            if key in p["groups"]:
                return p["groups"][key]
            if not create:
                return None
            d = {"settings": {}, "active": True if p["default_v4"] else None}
            p["peers"][key] = d
            p["order"].append(key)
            return d

        def handle_neighbor(p: dict, words: list[str], in_af: Optional[str]) -> None:
            key = words[1]
            if ":" in key:  # IPv6 not modelled (Batfish skips IPv6 peers here too)
                return
            sub = [x.lower() for x in words[2:]]
            if not sub:
                return
            if sub[0] == "peer-group" and len(words) == 3:  # neighbor G peer-group  (definition)
                p["groups"].setdefault(key, {"settings": {}, "active": None})
                return
            if sub[0] == "peer-group" and len(words) > 3:   # neighbor X peer-group G (membership)
                d = peer(p, key, True)
                d["settings"]["group"] = words[3]
                return
            create = sub[0] in ("remote-as", "inherit")
            d = peer(p, key, create)
            if d is None:
                return
            s = d["settings"]
            if sub[0] == "remote-as" and len(words) > 3:
                s["remote_as"] = parse_asn(words[3])
                if "alternate-as" in sub:       # Cisco_bgp.g4: REMOTE_AS remote (ALTERNATE_AS alt_ases+)?
                    s["alternate_as"] = [parse_asn(x) for x in words[3 + sub.index("alternate-as"):]
                                         if parse_asn(x) is not None]
            elif sub[0] == "activate":
                if in_af == "ipv4":
                    d["active"] = True
                elif in_af == "evpn":
                    s["evpn"] = True
            elif sub == ["shutdown"]:
                s["shutdown"] = True
            elif sub[0] == "update-source" and len(words) > 3:
                s["update_source"] = canonical(words[3], IOS)
            elif sub[0] == "description":
                s["description"] = " ".join(words[3:])
            elif sub[0] == "ebgp-multihop":
                s["ebgp_multihop"] = True
            elif sub[0] == "route-reflector-client":
                s["rr_client"] = True
            elif sub[0] == "send-community":
                s["send_community"] = True
            elif sub[0] == "route-map" and len(words) > 4:
                s["import" if sub[2] == "in" else "export"] = words[3]
            elif sub[0] == "local-as" and len(words) > 3:
                s["local_as"] = parse_asn(words[3])

        def walk(p: dict, lines: list[Line], in_af: Optional[str]) -> None:
            for ch in lines:
                w = ch.words
                low = [x.lower() for x in w]
                if low[0] == "address-family":
                    af = "ipv4" if low[1] == "ipv4" else ("evpn" if low[1:3] == ["l2vpn", "evpn"] else low[1])
                    if "vrf" in low:
                        vrf = w[low.index("vrf") + 1]
                        vp = proc(vrf)
                        vp["default_v4"] = True
                        walk(vp, ch.children, af)
                    else:
                        walk(p, ch.children, af)
                elif low[0] == "neighbor" and len(w) > 2:
                    if in_af not in (None, "ipv4", "evpn"):
                        continue
                    handle_neighbor(p, w, in_af)
                elif low[:2] == ["bgp", "router-id"] and len(w) > 2:
                    p["router_id"] = w[2]
                elif low[:2] == ["no", "bgp"] and low[2:4] == ["default", "ipv4-unicast"]:
                    pass   # Cisco_bgp.g4 no_bgp_default_ipv4_unicast_bgp_tail_null: Batfish ignores it
                elif low[:2] == ["no", "neighbor"] and len(w) > 3 and low[3] == "activate" and in_af in (None, "ipv4"):
                    d = peer(p, w[2], False)          # exitNo_neighbor_activate_rb_stanza
                    if d is not None:
                        d["active"] = False
                elif low[:3] == ["bgp", "bestpath", "compare-routerid"]:
                    p["tie"] = "ROUTER_ID"
                elif low[:4] == ["bgp", "bestpath", "as-path", "multipath-relax"]:
                    p["relax"] = True
                elif low[0] == "maximum-paths" and in_af in (None, "ipv4"):
                    if low[1] == "ibgp":
                        p["max_ibgp"] = int(low[2])
                    elif low[1] == "eibgp":
                        p["max_paths"] = int(low[2])
                    elif low[1].isdigit():
                        p["max_paths"] = int(low[1])
                elif low[:2] == ["bgp", "cluster-id"] and len(w) > 2:
                    p["cluster_id"] = w[2] if is_ipv4(w[2]) else str(ipaddress.IPv4Address(int(w[2])))
                elif low[:3] == ["bgp", "confederation", "identifier"]:
                    p["confed"] = parse_asn(w[3])
                elif low[:3] == ["bgp", "confederation", "peers"]:
                    p["confed_members"] = ",".join(w[3:])
                elif low[:3] == ["bgp", "listen", "range"] and "peer-group" in low:
                    pfx = w[3]
                    grp = w[low.index("peer-group") + 1]
                    d = {"settings": {"group": grp, "dynamic": pfx}, "active": True if p["default_v4"] else None}
                    p["peers"][pfx] = d
                    p["order"].append(pfx)

        root = proc(DEFAULT_VRF)
        walk(root, block.children, None)

        for vrf, p in procs.items():
            bp = BgpProcess(vrf=vrf, local_as=asn)
            # IOS: maximum-paths N sets both, then ebgp/ibgp specific
            bp.multipath_ebgp = p["max_paths"] > 1 or p["max_ebgp"] > 1
            bp.multipath_ibgp = p["max_paths"] > 1 or p["max_ibgp"] > 1
            bp.multipath_match_mode = "PATH_LENGTH" if p["relax"] else "EXACT_PATH"
            bp.tie_breaker = p["tie"] or "ARRIVAL_ORDER"
            bp.confederation_id = p["confed"] if p["confed"] and p["confed_members"] else None
            bp.confederation_members = p["confed_members"] if bp.confederation_id else None
            rid = p["router_id"] or procs[DEFAULT_VRF]["router_id"]
            bp.router_id = rid  # None -> inferred in finalize (IOS rule)
            bp.cluster_id = p["cluster_id"]
            for key in p["order"]:
                d = p["peers"][key]
                s = dict(d["settings"])
                active = d["active"]
                g = s.get("group")
                if g and g in p["groups"]:
                    gs = p["groups"][g]
                    for k, v in gs["settings"].items():
                        s.setdefault(k, v)
                    if active is None:
                        active = gs["active"]
                if not active or s.get("shutdown"):
                    continue
                if s.get("remote_as") is None:
                    self.node.warnings.append(f"No remote-as set for peer: {key}")
                    continue
                dyn = s.get("dynamic")
                peer_obj = BgpPeer(key=key, vrf=vrf)
                if dyn:
                    peer_obj.remote_prefix = str(ipaddress.IPv4Network(dyn, strict=False))
                    peer_obj.key = peer_obj.remote_prefix
                else:
                    peer_obj.remote_ip = key
                peer_obj.remote_as = format_int_space({s["remote_as"], *s.get("alternate_as", [])})
                peer_obj.local_as = s.get("local_as", asn)
                peer_obj.update_source = s.get("update_source")
                peer_obj.description = s.get("description")
                peer_obj.peer_group = g
                peer_obj.ebgp_multihop = bool(s.get("ebgp_multihop"))
                peer_obj.rr_client = bool(s.get("rr_client"))
                peer_obj.send_community = bool(s.get("send_community"))
                peer_obj.import_policy = [s["import"]] if s.get("import") in self.route_maps else []
                peer_obj.export_policy = [s["export"]] if s.get("export") in self.route_maps else []
                peer_obj.families = {"IPV4_UNICAST"}
                peer_obj.confederation = bp.confederation_id
                bp.peers[peer_obj.key] = peer_obj
            self.node.bgp[vrf] = bp

    # ---- NX-OS
    def _nxos_bgp(self, block: Line, asn: Optional[int]) -> None:
        templates: dict[str, dict] = {}

        def parse_peer(lines: list[Line]) -> dict:
            s: dict = {"afs": {}}
            for ch in lines:
                w = ch.words
                low = [x.lower() for x in w]
                if low[0] == "remote-as" and len(w) > 1:
                    s["remote_as"] = parse_asn(w[1]) if w[1].lower() != "route-map" else None
                elif low[:2] == ["inherit", "peer"] and len(w) > 2:
                    s["inherit"] = w[2]
                elif low[0] == "update-source" and len(w) > 1:
                    s["update_source"] = canonical(w[1], NXOS)
                elif low[0] == "description":
                    d = _rest(ch, 1)
                    s["description"] = d if 1 <= len(d) <= 80 else None   # BGP_NEIGHBOR_DESCRIPTION_LENGTH_RANGE
                elif low[0] == "ebgp-multihop" and len(w) > 1:
                    s["ebgp_multihop"] = int(w[1]) > 1
                elif low[0] == "local-as" and len(w) > 1:
                    s["local_as"] = parse_asn(w[1])
                elif low == ["shutdown"]:
                    s["shutdown"] = True
                elif low[0] == "address-family" and len(low) > 1:
                    key = "ipv4" if low[1:3] == ["ipv4", "unicast"] else (
                        "evpn" if low[1:3] == ["l2vpn", "evpn"] else ("ipv6" if low[1] == "ipv6" else None))
                    if key is None:
                        continue
                    af = s["afs"].setdefault(key, {})
                    for ac in ch.children:
                        aw = ac.words
                        al = [x.lower() for x in aw]
                        if al[0] == "route-map" and len(aw) > 2:
                            af["import" if al[2] == "in" else "export"] = aw[1]
                        elif al[0] == "send-community":
                            if len(al) == 1 or al[1] in ("both", "standard"):
                                af["send_community"] = True
                            af.setdefault("send_community", False)
                        elif al[0] == "route-reflector-client":
                            af["rr_client"] = True
            return s

        def inherit(s: dict, depth: int = 0) -> dict:
            t = s.get("inherit")
            if not t or t not in templates or depth > 5:
                return s
            base = inherit(templates[t], depth + 1)
            out = dict(base)
            out.update({k: v for k, v in s.items() if k != "afs"})
            afs = {k: dict(v) for k, v in base.get("afs", {}).items()}
            for k, v in s.get("afs", {}).items():
                afs.setdefault(k, {}).update(v)
            out["afs"] = afs
            out["inherit"] = s.get("inherit")
            return out

        vrf_blocks: list[tuple[str, list[Line]]] = [(DEFAULT_VRF, [])]
        for ch in block.children:
            low = [x.lower() for x in ch.words]
            if low[:2] == ["template", "peer"] and len(low) > 2:
                templates[ch.words[2]] = parse_peer(ch.children)
            elif low[0] == "vrf" and len(low) > 1:
                vrf_blocks.append((ch.words[1], ch.children))
            else:
                vrf_blocks[0][1].append(ch)

        global_rid = None
        for vrf, lines in vrf_blocks:
            bp = BgpProcess(vrf=vrf, local_as=asn)
            vrf_local_as = None
            max_e = max_i = 1
            have_af = False
            for ch in lines:
                w = ch.words
                low = [x.lower() for x in w]
                if low[0] == "router-id" and len(w) > 1:
                    bp.router_id = w[1]
                elif low[:2] == ["bestpath", "compare-routerid"]:
                    bp.tie_breaker = "ROUTER_ID"
                elif low[:3] == ["bestpath", "as-path", "multipath-relax"]:
                    bp.multipath_match_mode = "PATH_LENGTH"
                elif low[0] == "cluster-id" and len(w) > 1:
                    bp.cluster_id = w[1] if is_ipv4(w[1]) else str(ipaddress.IPv4Address(int(w[1])))
                elif low[0] == "local-as" and len(w) > 1:
                    vrf_local_as = parse_asn(w[1])
                elif low[:2] == ["confederation", "identifier"]:
                    bp.confederation_id = parse_asn(w[2])
                elif low[:2] == ["confederation", "peers"]:
                    bp.confederation_members = ",".join(w[2:])
                elif low[:3] == ["address-family", "ipv4", "unicast"]:
                    have_af = True
                    for ac in ch.children:
                        al = [x.lower() for x in ac.words]
                        if al[0] == "maximum-paths" and len(al) > 1:
                            if al[1] == "ibgp" and len(al) > 2:
                                max_i = int(al[2])
                            elif al[1].isdigit():
                                max_e = int(al[1])
                elif low[0] == "neighbor" and len(w) > 1:
                    key = w[1]
                    s = parse_peer(ch.children)
                    if len(w) > 3 and low[2] == "remote-as":
                        s["remote_as"] = parse_asn(w[3]) if low[3] != "route-map" else None
                    s = inherit(s)
                    if s.get("shutdown"):
                        continue
                    if not any(k in s["afs"] for k in ("ipv4", "ipv6", "evpn")):
                        self.node.warnings.append(f"No supported address-family configured for {key}")
                        continue
                    if ":" in key:
                        continue
                    p = BgpPeer(key=key, vrf=vrf)
                    if is_ipv4_prefix(key) and not key.endswith("/32"):
                        p.remote_prefix = str(ipaddress.IPv4Network(key, strict=False))
                        p.key = p.remote_prefix
                    elif is_ipv4(key.split("/")[0]):
                        p.remote_ip = key.split("/")[0]
                        p.key = p.remote_ip
                    else:
                        p.remote_interface = canonical(key, NXOS)
                        p.key = p.remote_interface
                    ra = s.get("remote_as")
                    p.remote_as = str(ra) if ra is not None else ("" if not p.is_dynamic else "")
                    p.local_as = s.get("local_as") or vrf_local_as or asn
                    p.update_source = s.get("update_source")
                    p.description = s.get("description")
                    p.peer_group = s.get("inherit")
                    p.ebgp_multihop = bool(s.get("ebgp_multihop"))
                    v4 = s["afs"].get("ipv4")
                    ev = s["afs"].get("evpn")
                    rep = v4 if v4 is not None else ev
                    if v4 is not None:
                        p.families.add("IPV4_UNICAST")
                    if ev is not None:
                        p.families.add("EVPN")
                    if rep is not None:
                        p.rr_client = bool(rep.get("rr_client"))
                        p.send_community = bool(rep.get("send_community"))
                        imp, exp = rep.get("import"), rep.get("export")
                        p.import_policy = [imp] if imp in self.route_maps else []
                        p.export_policy = [exp] if exp in self.route_maps else []
                    p.confederation = bp.confederation_id
                    bp.peers[p.key] = p
            if have_af:
                bp.multipath_ebgp = max_e > 1
                bp.multipath_ibgp = max_i > 1
            if vrf == DEFAULT_VRF:
                global_rid = bp.router_id
            bp.nx_vrf_local_as = vrf_local_as  # type: ignore[attr-defined]
            self.node.bgp[vrf] = bp
        if not any(b.peers for b in self.node.bgp.values()) and global_rid is None:
            pass
