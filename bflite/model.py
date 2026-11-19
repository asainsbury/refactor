"""Vendor-independent model, loosely mirroring Batfish's org.batfish.datamodel.

Every vendor parser fills these dataclasses; every question reads only these.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Optional

DEFAULT_VRF = "default"


@dataclass
class Address:
    ip: ipaddress.IPv4Address
    prefixlen: int

    @property
    def interface(self) -> ipaddress.IPv4Interface:
        return ipaddress.IPv4Interface(f"{self.ip}/{self.prefixlen}")

    @property
    def network(self) -> ipaddress.IPv4Network:
        return self.interface.network

    def __str__(self) -> str:
        return f"{self.ip}/{self.prefixlen}"


@dataclass
class Interface:
    name: str
    declared_names: list[str] = field(default_factory=list)
    description: Optional[str] = None
    vrf: str = DEFAULT_VRF
    admin_up: Optional[bool] = None          # None = vendor default, resolved in finalize
    addresses: list[Address] = field(default_factory=list)   # primary first
    link_local: Optional[str] = None         # e.g. Batfish's nve~<vni> tenant interfaces (169.254.0.1)
    unnumbered: Optional[str] = None

    # L2
    switchport: Optional[bool] = None        # None = vendor default
    switchport_mode: Optional[str] = None    # ACCESS / TRUNK / NONE / DYNAMIC_AUTO ...
    access_vlan: Optional[int] = None
    native_vlan: Optional[int] = None
    allowed_vlans: Optional[set[int]] = None
    trunk_encapsulation: str = "DOT1Q"
    encapsulation_vlan: Optional[int] = None
    channel_group: Optional[str] = None      # canonical parent aggregate name
    channel_group_members: list[str] = field(default_factory=list)
    mlag_id: Optional[int] = None
    portfast: bool = False
    autostate: bool = True

    # misc L3
    mtu: int = 1500
    bandwidth: Optional[float] = None        # bits/s
    speed: Optional[float] = None            # bits/s
    in_filter: Optional[str] = None
    out_filter: Optional[str] = None
    pbr_policy: Optional[str] = None
    dhcp_relay: list[str] = field(default_factory=list)
    hsrp_groups: list[str] = field(default_factory=list)
    hsrp_version: Optional[str] = None
    vrrp_groups: list[int] = field(default_factory=list)
    proxy_arp: Optional[bool] = None
    zone: Optional[str] = None

    # Junos unit -> parent, and SVI/IRB vlan
    parent: Optional[str] = None
    vlan_id: Optional[int] = None            # for Vlan<N> / irb.<N> interfaces
    kind: str = "PHYSICAL"                   # PHYSICAL, LOOPBACK, VLAN, AGGREGATED, LOGICAL, TUNNEL, NVE, MGMT

    inactive_reason_pre: Optional[str] = None  # vendor-conversion deactivation (e.g. INVALID, VRF_DOWN)

    # results of finalize()
    active: bool = True
    inactive_reason: Optional[str] = None

    @property
    def primary(self) -> Optional[Address]:
        return self.addresses[0] if self.addresses else None


@dataclass
class BgpPeer:
    key: str                                  # IP, prefix (dynamic) or interface name (unnumbered)
    vrf: str = DEFAULT_VRF
    remote_ip: Optional[str] = None
    remote_prefix: Optional[str] = None       # dynamic / listen-range peers
    remote_interface: Optional[str] = None    # unnumbered peers
    remote_as: Optional[str] = None           # str like "65001" or "65001-65010"
    local_as: Optional[int] = None
    local_ip: Optional[str] = None            # explicit local-address
    update_source: Optional[str] = None       # interface name
    description: Optional[str] = None
    peer_group: Optional[str] = None
    ebgp_multihop: bool = False
    rr_client: bool = False
    send_community: bool = False
    passive: bool = False
    shutdown: bool = False
    import_policy: list[str] = field(default_factory=list)
    export_policy: list[str] = field(default_factory=list)
    families: set[str] = field(default_factory=set)
    confederation: Optional[int] = None
    cluster_id: Optional[str] = None
    # set by finalize
    resolved_local_ip: Optional[str] = None

    @property
    def is_dynamic(self) -> bool:
        return self.remote_prefix is not None

    @property
    def is_unnumbered(self) -> bool:
        return self.remote_interface is not None


@dataclass
class BgpProcess:
    vrf: str = DEFAULT_VRF
    local_as: Optional[int] = None
    router_id: Optional[str] = None
    multipath_ebgp: bool = False
    multipath_ibgp: bool = False
    multipath_match_mode: str = "EXACT_PATH"
    tie_breaker: str = "ARRIVAL_ORDER"
    confederation_id: Optional[int] = None
    confederation_members: Optional[str] = None
    cluster_id: Optional[str] = None
    peers: dict[str, BgpPeer] = field(default_factory=dict)


@dataclass
class Vlan:
    vlan_id: int
    name: Optional[str] = None
    vni: Optional[int] = None


@dataclass
class Node:
    hostname: str
    vendor: str                               # CISCO_IOS / CISCO_NXOS / JUNIPER
    filename: str = ""
    interfaces: dict[str, Interface] = field(default_factory=dict)
    vrfs: set[str] = field(default_factory=lambda: {DEFAULT_VRF})
    bgp: dict[str, BgpProcess] = field(default_factory=dict)   # vrf -> process
    vlans: dict[int, Vlan] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def iface(self, name: str) -> Interface:
        if name not in self.interfaces:
            self.interfaces[name] = Interface(name=name, declared_names=[name])
        return self.interfaces[name]
