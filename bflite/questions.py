"""Question implementations returning pandas DataFrames with pybatfish-identical columns."""
from __future__ import annotations

import ipaddress
import re
from collections import defaultdict
from typing import Iterable, Optional


from .datamodel import Interface as NIP, ListWrapper
from .model import BgpPeer, Node
from .util import as_space_contains, format_int_space, iface_name_key, parse_vlan_list

AF_ORDER = ["IPV4_UNICAST", "IPV6_UNICAST", "EVPN"]


class Table:
    """Rows plus column order. No pandas needed; Session.answer().frame() converts on demand."""
    __slots__ = ("rows", "columns")

    def __init__(self, rows: list[dict], columns: list[str]):
        self.rows, self.columns = rows, columns

    def __len__(self) -> int:
        return len(self.rows)

    def to_frame(self):
        """Exactly like pybatfish's _rows_to_frame: object dtype, None kept, lists as ListWrapper."""
        import pandas as pd
        
        def conv(v):
            return ListWrapper(v) if isinstance(v, list) else v
        data = {c: pd.Series([conv(r.get(c)) for r in self.rows], dtype="object") for c in self.columns}
        return pd.DataFrame.from_dict(data, orient="columns", dtype="object").reindex(labels=self.columns,
                                                                                         axis="columns")


def _frame(rows: list[dict], columns: list[str]) -> Table:
    return Table(rows, columns)


# ----------------------------------------------------------------------------- specifiers
def resolve_nodes(nodes: dict[str, Node], spec: Optional[str | Iterable[str]]) -> list[str]:
    """Subset of Batfish node-specifier grammar: names, /regex/, comma-separated unions."""
    names = sorted(nodes)
    if spec is None or spec == "" or spec == ".*":
        return names
    parts = list(spec) if not isinstance(spec, str) else [p.strip() for p in spec.split(",")]
    out: set[str] = set()
    for p in parts:
        if p.startswith("/") and p.endswith("/") and len(p) > 1:
            rx = re.compile(p[1:-1], re.I)
            out |= {n for n in names if rx.search(n)}
        elif p.lower() in nodes:
            out.add(p.lower())
        else:
            try:  # bare regexes are accepted by older pybatfish
                rx = re.compile(f"^{p}$", re.I)
                out |= {n for n in names if rx.match(n)}
            except re.error:
                pass
    return sorted(out)


def _match_iface(spec: Optional[str], name: str) -> bool:
    if not spec:
        return True
    for p in [x.strip() for x in spec.split(",")]:
        if p.startswith("/") and p.endswith("/"):
            if re.search(p[1:-1], name, re.I):
                return True
        elif p.lower() == name.lower():
            return True
    return False


def _afs(s: Iterable[str]) -> list[str]:
    return [a for a in AF_ORDER if a in set(s)]


# ----------------------------------------------------------------------------- properties
def interface_properties(nodes: dict[str, Node], node_spec=None, interfaces=None, properties=None,
                         exclude_shut=False) -> Table:
    rows = []
    for n in resolve_nodes(nodes, node_spec):
        node = nodes[n]
        for name, i in sorted(node.interfaces.items()):
            if not _match_iface(interfaces, name):
                continue
            if exclude_shut and not i.active:
                continue
            ktype = {"MGMT": "PHYSICAL", "NVE": "UNKNOWN"}.get(i.kind, i.kind)
            if i.primary:
                primary_address, primary_network = str(i.primary), str(i.primary.network)
            elif i.link_local:   # LinkLocalAddress.toString(); not a concrete prefix, so no network
                primary_address, primary_network = f"link-local:{i.link_local}", None
            else:
                primary_address = primary_network = None
            row = {
                "Access_VLAN": i.access_vlan,
                "Active": i.active,
                "Admin_Up": bool(i.admin_up),
                # ConcreteInterfaceAddress order: IP, then prefix length (the primary isn't first)
                "All_Prefixes": [str(a) for a in sorted(i.addresses, key=lambda a: (int(a.ip), a.prefixlen))],
                "Allowed_VLANs": format_int_space(i.allowed_vlans) if i.switchport_mode == "TRUNK" else "",
                "Auto_State_VLAN": i.autostate,
                "Bandwidth": i.bandwidth,
                # Interface.setInterfaceType: false for PHYSICAL, null for every other type
                "Blacklisted": False if ktype == "PHYSICAL" else None,
                "Channel_Group": i.channel_group,
                "Channel_Group_Members": list(i.channel_group_members),
                "DHCP_Relay_Addresses": list(i.dhcp_relay),
                "Declared_Names": list(i.declared_names),
                "Description": i.description,
                "Encapsulation_VLAN": i.encapsulation_vlan,
                "HSRP_Groups": list(i.hsrp_groups),
                "HSRP_Version": i.hsrp_version,
                "Inactive_Reason": i.inactive_reason or "",
                "Incoming_Filter_Name": i.in_filter,
                "Interface_Type": ktype,
                "MLAG_ID": i.mlag_id,
                "MTU": i.mtu,
                "Native_VLAN": i.native_vlan,
                "Outgoing_Filter_Name": i.out_filter,
                "PBR_Policy_Name": i.pbr_policy,
                "Primary_Address": primary_address,
                "Primary_Network": primary_network,
                "Proxy_ARP": bool(i.proxy_arp),
                "Rip_Enabled": False,
                "Rip_Passive": False,
                "Spanning_Tree_Portfast": i.portfast,
                "Speed": i.speed,
                "Switchport": bool(i.switchport),
                "Switchport_Mode": i.switchport_mode or "NONE",
                "Switchport_Trunk_Encapsulation": i.trunk_encapsulation,
                "VRF": i.vrf,
                "VRRP_Groups": sorted(i.vrrp_groups),
                "Zone_Name": i.zone,
            }
            if properties:
                rx = re.compile(properties, re.I)
                row = {k: v for k, v in row.items() if rx.search(k)}
            else:   # InterfacePropertySpecifier.create: Interface_Type only when asked for by name
                del row["Interface_Type"]
            rows.append({"Interface": NIP(n, name), **dict(sorted(row.items()))})
    cols = ["Interface"] + (sorted(rows[0].keys() - {"Interface"}) if rows else [])
    return _frame(rows, columns=cols)


def ip_owners(nodes: dict[str, Node], duplicates_only=False, ips=None) -> Table:
    owners: dict[str, set[str]] = defaultdict(set)
    for n, node in nodes.items():
        for i in node.interfaces.values():
            for a in i.addresses:
                owners[str(a.ip)].add(n)
    rows = []
    for n in sorted(nodes):
        for name, i in sorted(nodes[n].interfaces.items()):
            for a in i.addresses:
                if duplicates_only and len(owners[str(a.ip)]) < 2:
                    continue
                if ips and ipaddress.IPv4Address(a.ip) not in ipaddress.IPv4Network(ips, strict=False):
                    continue
                rows.append({"Node": n, "VRF": i.vrf, "Interface": name, "IP": str(a.ip),
                             "Mask": a.prefixlen, "Active": i.active})
    return _frame(rows, columns=["Node", "VRF", "Interface", "IP", "Mask", "Active"])


def switched_vlan_properties(nodes: dict[str, Node], node_spec=None, interfaces=None, vlans=None,
                             exclude_shut=False) -> Table:
    vlan_space = parse_vlan_list(vlans) if vlans else set(range(1, 4095))
    rows = []
    for n in resolve_nodes(nodes, node_spec):
        node = nodes[n]
        by_vlan: dict[int, list] = {}
        vnis: dict[int, int] = {}
        for v in node.vlans.values():
            if v.vni is not None and v.vlan_id in vlan_space:
                vnis[v.vlan_id] = v.vni
                by_vlan.setdefault(v.vlan_id, [])
        for name, i in sorted(node.interfaces.items()):
            if not _match_iface(interfaces, name) or (exclude_shut and not i.active):
                continue
            if i.kind != "VLAN" and not i.switchport:
                continue
            ids: Iterable[int] = []
            if i.kind == "VLAN":
                ids = [i.vlan_id] if i.vlan_id is not None else []
            elif i.switchport_mode == "ACCESS":
                ids = [i.access_vlan] if i.access_vlan is not None else []
            elif i.switchport_mode == "TRUNK":
                ids = sorted(i.allowed_vlans or [])
            for v in ids:
                if v in vlan_space:
                    by_vlan.setdefault(v, []).append(NIP(n, name))
        for v in sorted(by_vlan):
            ifs = sorted(set(by_vlan[v]), key=lambda x: iface_name_key(x.interface))   # NodeInterfacePair order
            rows.append({"Node": n, "VLAN_ID": v, "Interfaces": ifs, "VXLAN_VNI": vnis.get(v)})
    return _frame(rows, columns=["Node", "VLAN_ID", "Interfaces", "VXLAN_VNI"])


# ----------------------------------------------------------------------------- topology
def _l3_edges(nodes: dict[str, Node]) -> list[tuple]:
    """Port of TopologyUtil.synthesizeL3Edges (no layer-1 topology => every pair may be adjacent)."""
    buckets: dict[ipaddress.IPv4Network, list] = defaultdict(list)
    for n, node in nodes.items():
        for i in node.interfaces.values():
            if i.kind == "LOOPBACK" or not i.active or i.kind == "TUNNEL":
                continue
            for a in i.addresses:
                if a.prefixlen < 32:
                    buckets[a.network].append((n, i))
    edges = set()
    for p, members in buckets.items():
        cands = []
        for length in range(0, 32):
            q = ipaddress.IPv4Network(f"{p.network_address}/{length}", strict=False)
            for (n2, i2) in buckets.get(q, []):
                if any(a.ip in p for a in i2.addresses):
                    cands.append((n2, i2))
        for (n1, i1) in members:
            for (n2, i2) in cands:
                if n1 == n2 and i1.vrf == i2.vrf:
                    continue
                if {a.ip for a in i1.addresses} & {a.ip for a in i2.addresses}:
                    continue
                edges.add((n1, i1.name, n2, i2.name))
    return sorted(edges)


def layer3_edges(nodes: dict[str, Node], node_spec=None, remote_node_spec=None) -> Table:
    ns = set(resolve_nodes(nodes, node_spec))
    rs = set(resolve_nodes(nodes, remote_node_spec))
    rows = []
    for n1, if1, n2, if2 in _l3_edges(nodes):
        if n1 not in ns or n2 not in rs:
            continue
        rows.append({"Interface": NIP(n1, if1),
                     "IPs": [str(a.ip) for a in nodes[n1].interfaces[if1].addresses],
                     "Remote_Interface": NIP(n2, if2),
                     "Remote_IPs": [str(a.ip) for a in nodes[n2].interfaces[if2].addresses]})
    return _frame(rows, columns=["Interface", "IPs", "Remote_Interface", "Remote_IPs"])


# ----------------------------------------------------------------------------- BGP config
def bgp_process_configuration(nodes, node_spec=None, properties=None) -> Table:
    rows = []
    for n in resolve_nodes(nodes, node_spec):
        for vrf, bp in sorted(nodes[n].bgp.items()):
            rows.append({
                "Node": n, "VRF": vrf, "Router_ID": bp.router_id,
                "Confederation_ID": bp.confederation_id, "Confederation_Members": bp.confederation_members,
                "Multipath_EBGP": bp.multipath_ebgp, "Multipath_IBGP": bp.multipath_ibgp,
                "Multipath_Match_Mode": bp.multipath_match_mode,
                "Neighbors": list(bp.peers.keys()),
                "Route_Reflector": any(p.rr_client and "IPV4_UNICAST" in p.families for p in bp.peers.values()),
                "Tie_Breaker": bp.tie_breaker,
            })
    cols = ["Node", "VRF", "Router_ID", "Confederation_ID", "Confederation_Members", "Multipath_EBGP",
            "Multipath_IBGP", "Multipath_Match_Mode", "Neighbors", "Route_Reflector", "Tie_Breaker"]
    return _filter_props(_frame(rows, columns=cols), properties, keep=["Node", "VRF", "Router_ID"])


def bgp_peer_configuration(nodes, node_spec=None, properties=None) -> Table:
    rows = []
    for n in resolve_nodes(nodes, node_spec):
        for vrf, bp in sorted(nodes[n].bgp.items()):
            for p in bp.peers.values():
                rows.append({
                    "Node": n, "VRF": vrf, "Session_VRF": None, "Local_AS": p.local_as,
                    "Local_IP": None if p.is_unnumbered else p.resolved_local_ip,
                    "Local_Interface": p.remote_interface,
                    "Confederation": p.confederation, "Remote_AS": p.remote_as,
                    "Remote_IP": None if p.is_unnumbered else (p.remote_prefix or p.remote_ip),
                    "Description": p.description, "Route_Reflector_Client": p.rr_client,
                    "Cluster_ID": p.cluster_id, "Peer_Group": p.peer_group,
                    "Import_Policy": sorted(p.import_policy), "Export_Policy": sorted(p.export_policy),
                    "Send_Community": p.send_community if p.families else False,
                    "Is_Passive": p.is_dynamic,
                })
    cols = ["Node", "VRF", "Session_VRF", "Local_AS", "Local_IP", "Local_Interface", "Confederation", "Remote_AS",
            "Remote_IP", "Description", "Route_Reflector_Client", "Cluster_ID", "Peer_Group", "Import_Policy",
            "Export_Policy", "Send_Community", "Is_Passive"]
    df = _frame(rows, columns=cols)
    return _filter_props(df, properties, keep=["Node", "VRF", "Local_Interface", "Remote_IP"])


def _filter_props(t: Table, properties: Optional[str], keep: list[str]) -> Table:
    if not properties:
        return t
    rx = re.compile(properties, re.I)
    cols = [c for c in t.columns if c in keep or rx.search(c)]
    return Table([{c: r.get(c) for c in cols} for r in t.rows], cols)


# ----------------------------------------------------------------------------- BGP sessions
class _Topo:
    """Configured BGP topology (BgpTopologyUtils.initBgpTopology with keepInvalid=true, no reachability)."""

    def __init__(self, nodes: dict[str, Node]):
        self.nodes = nodes
        self.ip_vrf_owners: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        for n, node in nodes.items():
            for i in node.interfaces.values():
                if i.active:
                    for a in i.addresses:
                        self.ip_vrf_owners[str(a.ip)][n].add(i.vrf)
        self.peers: dict[tuple, BgpPeer] = {}
        for n, node in nodes.items():
            for vrf, bp in node.bgp.items():
                for k, p in bp.peers.items():
                    self.peers[(n, vrf, k)] = p
        self.out: dict[tuple, dict[tuple, dict]] = defaultdict(dict)
        self.inn: dict[tuple, dict[tuple, dict]] = defaultdict(dict)
        self._build()

    def confed_members(self, pid) -> Optional[str]:
        bp = self.nodes[pid[0]].bgp.get(pid[1])
        return bp.confederation_members if bp else None

    def as_pair(self, a: BgpPeer, aid, b: BgpPeer, bid) -> Optional[tuple[int, int]]:
        la, lb = a.local_as, b.local_as
        if la is None or lb is None:
            return None
        ca, cb = a.confederation, b.confederation
        if ca is None and cb is None:
            if as_space_contains(b.remote_as, la) and as_space_contains(a.remote_as, lb):
                return la, lb
            return None
        if ca is not None and cb is not None:
            if ca == cb:
                return (la, lb) if as_space_contains(b.remote_as, la) and as_space_contains(a.remote_as, lb) else None
            return (ca, cb) if as_space_contains(b.remote_as, ca) and as_space_contains(a.remote_as, cb) else None
        if ca is not None:
            mem = self.confed_members(aid)
            if mem and as_space_contains(mem, lb):
                return None
            return (ca, lb) if as_space_contains(b.remote_as, ca) and as_space_contains(a.remote_as, lb) else None
        mem = self.confed_members(bid)
        if mem and as_space_contains(mem, la):
            return None
        return (la, cb) if as_space_contains(b.remote_as, la) and as_space_contains(a.remote_as, cb) else None

    def potential_local_ips(self, pid, p: BgpPeer) -> set[str]:
        if p.resolved_local_ip:
            return {p.resolved_local_ip}
        # Approximation of SourceIpInference without a FIB: connected-subnet source address only
        if p.remote_ip is None:
            return set()
        rip = ipaddress.IPv4Address(p.remote_ip)
        node = self.nodes[pid[0]]
        return {str(a.ip) for i in node.interfaces.values() if i.vrf == pid[1] and i.active
                for a in i.addresses if rip in a.network}

    def _build(self) -> None:
        for aid, a in self.peers.items():
            if a.is_dynamic or a.is_unnumbered:
                continue
            local_ips = self.potential_local_ips(aid, a)
            if not local_ips or a.local_as is None or a.remote_ip is None or not a.remote_as:
                continue
            owners = self.ip_vrf_owners.get(a.remote_ip, {})
            for node_name, vrfs in owners.items():
                for vrf in vrfs:
                    for bid, b in self.peers.items():
                        if bid[0] != node_name or bid[1] != vrf or b.is_unnumbered:
                            continue
                        if bid[0] == aid[0] and bid[1] == aid[1]:
                            continue
                        pair = self.as_pair(a, aid, b, bid)
                        if pair is None:
                            continue
                        if b.is_dynamic:
                            net = ipaddress.IPv4Network(b.remote_prefix)
                            feasible = {ip for ip in local_ips if ipaddress.IPv4Address(ip) in net}
                        else:
                            feasible = {b.remote_ip} if b.remote_ip in local_ips else set()
                        for lip in feasible:
                            fam = _afs(a.families & b.families)
                            fwd = {"local_as": pair[0], "remote_as": pair[1], "local_ip": lip, "remote_ip": a.remote_ip,
                                   "afs": fam, "type": session_type(a)}
                            rev = {"local_as": pair[1], "remote_as": pair[0], "local_ip": a.remote_ip, "remote_ip": lip,
                                   "afs": fam, "type": session_type(a)}
                            self.out[aid][bid] = fwd
                            self.inn[bid][aid] = fwd
                            self.out[bid][aid] = rev
                            self.inn[aid][bid] = rev

    def adjacent(self, pid) -> set:
        return set(self.out.get(pid, {})) | set(self.inn.get(pid, {}))


def session_type(p: BgpPeer) -> str:
    if p.local_as is None or not p.remote_as:
        return "UNSET"
    las = p.local_as
    if p.confederation is not None and p.remote_as == str(p.confederation):
        las = p.confederation
    same = p.remote_as == str(las)
    if p.is_unnumbered:
        return "IBGP_UNNUMBERED" if same else "EBGP_UNNUMBERED"
    if same:
        return "IBGP"
    return "EBGP_MULTIHOP" if p.ebgp_multihop else "EBGP_SINGLEHOP"


def _configured_status(topo: _Topo, pid, p: BgpPeer) -> str:
    st = session_type(p)
    if p.resolved_local_ip is None:
        return "LOCAL_IP_UNKNOWN_STATICALLY" if st in ("EBGP_MULTIHOP", "IBGP") else "NO_LOCAL_IP"
    if p.local_as is None:
        return "NO_LOCAL_AS"
    if p.remote_ip is None:
        return "NO_REMOTE_IP"
    if not p.remote_as:
        return "NO_REMOTE_AS"
    if pid[1] not in topo.ip_vrf_owners.get(p.resolved_local_ip, {}).get(pid[0], set()):
        return "INVALID_LOCAL_IP"
    if p.remote_ip not in topo.ip_vrf_owners:
        return "UNKNOWN_REMOTE"
    if not topo.adjacent(pid):
        return "HALF_OPEN"
    if len(topo.out.get(pid, {})) > 1:
        return "MULTIPLE_REMOTES"
    return "UNIQUE_MATCH"


_SESSION_COLS = ["Node", "VRF", "Session_VRF", "Local_AS", "Local_Interface", "Local_IP", "Remote_AS", "Remote_Node",
                 "Remote_Interface", "Remote_IP", "Address_Families", "Session_Type"]


def _base_row(pid, p: BgpPeer) -> dict:
    return {"Node": pid[0], "VRF": pid[1], "Session_VRF": None, "Local_AS": p.local_as, "Local_Interface": None,
            "Local_IP": p.resolved_local_ip, "Remote_AS": p.remote_as, "Remote_Node": None,
            "Remote_Interface": None, "Remote_IP": p.remote_ip, "Address_Families": [],
            "Session_Type": session_type(p)}


def _passive_broken(p: BgpPeer) -> Optional[str]:
    if p.local_as is None:
        return "NO_LOCAL_AS"
    if p.remote_prefix is None:
        return "NO_REMOTE_PREFIX"
    if not p.remote_as:
        return "NO_REMOTE_AS"
    return None


def _session_rows(nodes, node_spec, remote_node_spec, status_filter, type_filter, established: bool):
    topo = _Topo(nodes)
    ns = set(resolve_nodes(nodes, node_spec))
    rs = set(resolve_nodes(nodes, remote_node_spec)) if remote_node_spec else None
    reach = _Reachability(nodes) if established else None
    col = "Established_Status" if established else "Configured_Status"
    rows = []
    for pid, p in sorted(topo.peers.items()):
        if p.is_dynamic:
            base = _base_row(pid, p)
            base.update({"Local_IP": None, "Remote_IP": p.remote_prefix, "Session_Type": "UNSET"})
            broken = _passive_broken(p)
            if broken:
                rows.append({**base, col: "NOT_COMPATIBLE" if established else broken})
                continue
            remotes = sorted(topo.adjacent(pid))
            if not established:
                remotes = [r for r in remotes if len(topo.inn.get(r, {})) == 1]
            if not remotes:
                rows.append({**base, col: "NOT_ESTABLISHED" if established else "NO_MATCH_FOUND"})
                continue
            for rid in remotes:
                props = topo.out[pid][rid]
                status = "DYNAMIC_MATCH"
                if established:
                    status = ("ESTABLISHED" if reach.ok(rid, topo.peers[rid], props["remote_ip"], pid)
                              else "NOT_ESTABLISHED")
                rows.append({**base, "Address_Families": props["afs"], "Local_IP": props["local_ip"],
                             "Local_AS": props["local_as"], "Remote_AS": str(props["remote_as"]),
                             "Remote_Node": rid[0], "Remote_IP": props["remote_ip"],
                             "Session_Type": session_type(topo.peers[rid]), col: status})
            continue
        if p.is_unnumbered:
            base = _base_row(pid, p)
            base.update({"Local_IP": None, "Remote_IP": None, "Local_Interface": NIP(pid[0], p.remote_interface)})
            st = "NO_LOCAL_AS" if p.local_as is None else ("NO_REMOTE_AS" if not p.remote_as else "HALF_OPEN")
            rows.append({**base, col: ("NOT_COMPATIBLE" if established else st)})
            continue
        base = _base_row(pid, p)
        cstat = _configured_status(topo, pid, p)
        if not established:
            if cstat == "UNIQUE_MATCH":
                rid = next(iter(topo.adjacent(pid)))
                props = topo.out[pid].get(rid) or topo.inn[pid].get(rid)
                base.update({"Remote_Node": rid[0], "Remote_AS": str(props["remote_as"]),
                             "Local_AS": props["local_as"], "Address_Families": props["afs"]})
            rows.append({**base, col: cstat})
        else:
            # With a dataplane, BgpTopologyUtils infers a missing local IP from the FIB (InferFromFib), so a peer
            # without a configured local IP (Junos without local-address) can still be ESTABLISHED. The candidate
            # edges already carry the connected-subnet source IP from _Topo.potential_local_ips.
            out_edges = topo.out.get(pid, {})
            est = [r for r in out_edges if reach.ok(pid, p, out_edges[r]["local_ip"], r)]
            if len(est) == 1 and cstat in ("UNIQUE_MATCH", "MULTIPLE_REMOTES", "NO_LOCAL_IP",
                                           "LOCAL_IP_UNKNOWN_STATICALLY"):
                rid = est[0]
                props = topo.out[pid][rid]
                base.update({"Remote_Node": rid[0], "Remote_AS": str(props["remote_as"]),
                             "Local_AS": props["local_as"], "Local_IP": props["local_ip"],
                             "Address_Families": props["afs"], "Remote_IP": props["remote_ip"]})
                rows.append({**base, col: "ESTABLISHED"})
            elif cstat == "UNIQUE_MATCH":
                rid = next(iter(topo.adjacent(pid)))
                props = topo.out[pid].get(rid) or topo.inn[pid].get(rid)
                base.update({"Remote_Node": rid[0], "Remote_AS": str(props["remote_as"]),
                             "Local_AS": props["local_as"], "Address_Families": props["afs"]})
                rows.append({**base, col: "NOT_ESTABLISHED"})
            else:
                rows.append({**base, col: "NOT_COMPATIBLE"})
    out = []
    for r in rows:
        if r["Node"] not in ns:
            continue
        if rs is not None and (r["Remote_Node"] is None or r["Remote_Node"] not in rs):
            continue
        if status_filter and not re.fullmatch(status_filter, r[col], re.I):
            continue
        if type_filter and not re.fullmatch(type_filter, r["Session_Type"], re.I):
            continue
        out.append(r)
    return _frame(out, columns=_SESSION_COLS + [col])


class _Reachability:
    """Approximation of Batfish's traceroute-based session check (no FIB/ACL evaluation).

    * single-hop eBGP: both IPs must sit on the two ends of a synthesized L3 edge
    * iBGP / multihop: the two (node, vrf) endpoints must be connected through L3 edges
    """

    def __init__(self, nodes: dict[str, Node]):
        self.nodes = nodes
        self.edges = _l3_edges(nodes)
        self.adj: dict[tuple, set] = defaultdict(set)
        for n1, i1, n2, i2 in self.edges:
            v1 = nodes[n1].interfaces[i1].vrf
            v2 = nodes[n2].interfaces[i2].vrf
            self.adj[(n1, v1)].add((n2, v2))
            self.adj[(n2, v2)].add((n1, v1))
        self.edge_ips = set()
        for n1, i1, n2, i2 in self.edges:
            for a in nodes[n1].interfaces[i1].addresses:
                for b in nodes[n2].interfaces[i2].addresses:
                    self.edge_ips.add((n1, str(a.ip), n2, str(b.ip)))

    def _connected(self, a, b) -> bool:
        seen, stack = {a}, [a]
        while stack:
            x = stack.pop()
            if x == b:
                return True
            for y in self.adj.get(x, ()):
                if y not in seen:
                    seen.add(y)
                    stack.append(y)
        return False

    def ok(self, pid, p: BgpPeer, local_ip: Optional[str], rid=None) -> bool:
        st = session_type(p)
        lip = local_ip or p.resolved_local_ip
        if rid is None:
            return False
        if st == "EBGP_SINGLEHOP":
            return (pid[0], lip, rid[0], p.remote_ip) in self.edge_ips
        return self._connected((pid[0], pid[1]), (rid[0], rid[1]))


def bgp_session_compatibility(nodes, node_spec=None, remote_node_spec=None, status=None, type=None):
    return _session_rows(nodes, node_spec, remote_node_spec, status, type, established=False)


def bgp_session_status(nodes, node_spec=None, remote_node_spec=None, status=None, type=None):
    return _session_rows(nodes, node_spec, remote_node_spec, status, type, established=True)
