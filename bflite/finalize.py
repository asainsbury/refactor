"""Snapshot post-processing, mirroring Batfish.updateBlacklistedAndInactiveConfigs,
InterfaceDependencies, disableUnusableVlanInterfaces, computeAggregatedInterfaceBandwidths and the
vendor router-id / BGP local-IP inference."""
from __future__ import annotations

import ipaddress
import re
from typing import Optional

from .model import Interface, Node

ADMIN_DOWN = "Administratively down"
IGNORE_MGMT = "Ignored management interface"
NO_MEMBERS = "No configured member interfaces"
NO_ACTIVE_MEMBERS = "No operational member interfaces"
PARENT_DOWN = "Parent interface is down"
AUTOSTATE_FAILURE = "Autostate failure"
INVALID = "Invalid configuration"

_MGMT_IFACE = re.compile(r"(\Amgmt)|(\Amanagement)|(\Afxp0)|(\Aem0)|(\Ame0)|(\Avme)|(\Awlan-ap)|(\Aeth\d+-mgmt\d+)", re.I)
_MGMT_VRF = re.compile(r"(\Amgmt)|(\Amanagement)", re.I)


def _deactivate(itf: Interface, reason: str) -> None:
    if itf.active:
        itf.active = False
        itf.inactive_reason = reason


def finalize_node(node: Node, ignore_mgmt: bool = True) -> None:
    ifs = node.interfaces

    # aggregate membership
    for itf in ifs.values():
        itf.channel_group_members = []
    for itf in ifs.values():
        if itf.channel_group and itf.channel_group in ifs:
            ifs[itf.channel_group].channel_group_members.append(itf.name)
    for itf in ifs.values():
        itf.channel_group_members.sort()

    # 1. conversion-time deactivation
    for itf in ifs.values():
        itf.active, itf.inactive_reason = True, None
        if not itf.admin_up:
            _deactivate(itf, ADMIN_DOWN)
        elif itf.inactive_reason_pre:
            _deactivate(itf, itf.inactive_reason_pre)
        elif node.vendor == "CISCO_IOS" and itf.channel_group and itf.channel_group not in ifs:
            _deactivate(itf, INVALID)

    # 2. management interfaces
    if ignore_mgmt:
        for itf in ifs.values():
            if itf.active and (_MGMT_IFACE.search(itf.name) or _MGMT_VRF.search(itf.vrf)):
                _deactivate(itf, IGNORE_MGMT)

    # 3. interface dependencies (aggregates, sub-interfaces / units)
    for itf in ifs.values():
        if itf.kind == "AGGREGATED" and not itf.channel_group_members:
            _deactivate(itf, NO_MEMBERS)
    changed = True
    while changed:
        changed = False
        for itf in ifs.values():
            if not itf.active:
                continue
            if itf.kind == "AGGREGATED" and itf.channel_group_members:
                if all(not ifs[m].active for m in itf.channel_group_members):
                    _deactivate(itf, NO_ACTIVE_MEMBERS)
                    changed = True
            elif itf.parent and itf.parent in ifs and not ifs[itf.parent].active:
                _deactivate(itf, PARENT_DOWN)
                changed = True

    # 4. autostate on VLAN interfaces
    vlan_ifaces = {i.vlan_id: i for i in ifs.values() if i.active and i.kind == "VLAN" and i.vlan_id is not None}
    counts = {v: 0 for v in vlan_ifaces}
    for itf in ifs.values():
        if not itf.active or (itf.kind == "VLAN" and itf.vlan_id is not None):
            continue
        vlans: set[int] = set()
        if itf.switchport_mode == "TRUNK":
            vlans |= set(itf.allowed_vlans) if itf.allowed_vlans else set(vlan_ifaces)
            if itf.native_vlan is not None:
                vlans.add(itf.native_vlan)
        elif itf.switchport_mode == "ACCESS" and itf.access_vlan is not None:
            vlans.add(itf.access_vlan)
        for v in vlans:
            counts[v] = counts.get(v, 0) + 1
    for v, n in counts.items():
        if n == 0 and 1 <= v <= 4094:
            i = vlan_ifaces.get(v)
            if i is not None and i.autostate:
                _deactivate(i, AUTOSTATE_FAILURE)

    # 5. aggregate bandwidths (after deactivation, as in Batfish.postProcessSnapshot)
    for itf in ifs.values():
        if itf.kind == "AGGREGATED":
            itf.bandwidth = float(sum((ifs[m].bandwidth or 0.0) for m in itf.channel_group_members if ifs[m].active))
    for itf in ifs.values():
        if itf.kind == "AGGREGATE_CHILD" and itf.parent in ifs:
            itf.bandwidth = ifs[itf.parent].bandwidth or 0.0

    _finalize_bgp(node)


# ------------------------------------------------------------------------------------- BGP
def _vrf_ifaces(node: Node, vrf: str) -> list[Interface]:
    return [i for _, i in sorted(node.interfaces.items()) if i.vrf == vrf]


def _infer_router_id(node: Node, vrf: str) -> str:
    ifs = _vrf_ifaces(node, vrf)
    if node.vendor == "CISCO_IOS":
        best = 0
        for i in ifs:
            if i.name.startswith("Loopback") and i.primary:
                best = max(best, int(i.primary.ip))
        if best == 0:
            for i in ifs:
                if i.primary:
                    best = max(best, int(i.primary.ip))
        return str(ipaddress.IPv4Address(best))
    if node.vendor == "CISCO_NXOS":
        with_ip = {i.name: i for i in ifs if i.primary}
        if not with_ip:
            return "0.0.0.0"
        if "loopback0" in with_ip:
            return str(with_ip["loopback0"].primary.ip)
        loops = [int(i.primary.ip) for i in with_ip.values() if i.kind == "LOOPBACK"]
        if loops:
            return str(ipaddress.IPv4Address(min(loops)))
        return str(ipaddress.IPv4Address(min(int(i.primary.ip) for i in with_ip.values())))
    # Junos
    loops = [i for i in ifs if i.name.lower().startswith("lo")]
    cands = loops if loops else ifs
    ips = [int(i.primary.ip) for i in cands if i.active and i.primary]
    return str(ipaddress.IPv4Address(min(ips))) if ips else "0.0.0.0"


def _resolve_local_ip(node: Node, peer, vendor: str) -> Optional[str]:
    if vendor == "JUNIPER":
        return peer.local_ip
    ifs = _vrf_ifaces(node, peer.vrf)
    if peer.update_source:
        src = node.interfaces.get(peer.update_source)
        if src is None or src.vrf != peer.vrf:
            return None
        return str(src.primary.ip) if src.primary else None
    if peer.is_dynamic or peer.is_unnumbered or peer.remote_ip is None:
        return None
    rip = ipaddress.IPv4Address(peer.remote_ip)
    match = None
    for i in ifs:
        for a in i.addresses:
            if rip in a.network:
                if vendor == "CISCO_NXOS":
                    return str(a.ip)          # first match
                match = str(a.ip)             # IOS: last match wins
    return match


def _finalize_bgp(node: Node) -> None:
    for vrf, bp in node.bgp.items():
        if bp.router_id is None:   # explicit (or IOS default-VRF) router-id was set by the parser
            bp.router_id = _infer_router_id(node, vrf)
        for p in bp.peers.values():
            p.resolved_local_ip = _resolve_local_ip(node, p, node.vendor)
            if node.vendor == "JUNIPER":
                if not p.rr_client:
                    p.cluster_id = None
            else:
                p.cluster_id = (bp.cluster_id or bp.router_id) if p.rr_client else None
