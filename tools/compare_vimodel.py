"""Compare bflite's model against a Batfish vendor-independent model dump (viModel answer JSON).

Usage: python tools/compare_vimodel.py <snapshot_dir> <viModel.json>

A viModel dump is what `bf.q.viModel().answer()` (or the Batfish ref-test file
tests/basic/viModel.ref) returns. Prints every field that differs.
"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, __file__.rsplit("/tools/", 1)[0])
from bflite import load_snapshot  # noqa: E402


def main(snapshot: str, vimodel: str) -> int:
    ours = load_snapshot(snapshot)
    ref = json.load(open(vimodel))
    if isinstance(ref, list):
        ref = ref[0]
    ref = ref["nodes"]
    diffs = 0
    checked = 0

    def diff(where: str, field: str, mine, theirs) -> None:
        nonlocal diffs, checked
        checked += 1
        if mine != theirs:
            diffs += 1
            print(f"{where:45s} {field:22s} ours={mine!r:40s} batfish={theirs!r}")

    for host, rn in sorted(ref.items()):
        if host not in ours:
            print(f"missing node {host}")
            diffs += 1
            continue
        node = ours[host]
        for iname, ri in sorted(rn.get("interfaces", {}).items()):
            w = f"{host}[{iname}]"
            mi = node.interfaces.get(iname)
            if mi is None:
                print(f"{w:45s} missing interface")
                diffs += 1
                continue
            diff(w, "adminUp", bool(mi.admin_up), ri.get("adminUp"))
            diff(w, "allPrefixes", [str(a) for a in mi.addresses], ri.get("allPrefixes") or [])
            diff(w, "vrf", mi.vrf, ri.get("vrf"))
            diff(w, "bandwidth", mi.bandwidth, ri.get("bandwidth"))
            diff(w, "speed", mi.speed, ri.get("speed"))
            diff(w, "mtu", mi.mtu, ri.get("mtu"))
            diff(w, "proxyArp", bool(mi.proxy_arp), ri.get("proxyArp"))
            diff(w, "switchport", bool(mi.switchport), ri.get("switchport"))
            diff(w, "switchportMode", mi.switchport_mode, ri.get("switchportMode"))
            diff(w, "accessVlan", mi.access_vlan, ri.get("accessVlan"))
            diff(w, "nativeVlan", mi.native_vlan, ri.get("nativeVlan"))
            diff(w, "incomingFilter", mi.in_filter, ri.get("incomingFilter"))
            diff(w, "outgoingFilter", mi.out_filter, ri.get("outgoingFilter"))
            diff(w, "description", mi.description, ri.get("description"))
            diff(w, "autostate", mi.autostate, ri.get("autostate"))
            diff(w, "portfast", mi.portfast, ri.get("spanningTreePortfast"))
        extra = set(node.interfaces) - set(rn.get("interfaces", {}))
        for e in sorted(extra):
            print(f"{host}[{e}] extra interface (not in Batfish)")
            diffs += 1
        for vrf, rv in rn.get("vrfs", {}).items():
            rbp = rv.get("bgpProcess")
            mbp = node.bgp.get(vrf)
            if rbp is None:
                if mbp is not None and mbp.peers:
                    print(f"{host}:{vrf} extra bgp process")
                    diffs += 1
                continue
            w = f"{host}:{vrf}"
            if mbp is None:
                print(f"{w} missing bgp process")
                diffs += 1
                continue
            diff(w, "routerId", mbp.router_id, rbp.get("routerId"))
            diff(w, "multipathEbgp", mbp.multipath_ebgp, rbp.get("multipathEbgp"))
            diff(w, "multipathIbgp", mbp.multipath_ibgp, rbp.get("multipathIbgp"))
            diff(w, "multipathMode", mbp.multipath_match_mode, rbp.get("multipathEquivalentAsPathMatchMode"))
            neigh = dict(rbp.get("neighbors", {}))
            neigh.update(rbp.get("passiveNeighbors", {}) or {})
            neigh.update(rbp.get("interfaceNeighbors", {}) or {})
            for key, rp in sorted(neigh.items()):
                k = key.split("/")[0] if key.endswith("/32") else key
                mp = mbp.peers.get(k)
                pw = f"{w} {k}"
                if mp is None:
                    print(f"{pw:45s} missing peer")
                    diffs += 1
                    continue
                diff(pw, "localIp", mp.resolved_local_ip, rp.get("localIp"))
                diff(pw, "localAs", mp.local_as, rp.get("localAs"))
                diff(pw, "remoteAsns", mp.remote_as, rp.get("remoteAsns"))
                diff(pw, "group", mp.peer_group, rp.get("group"))
                diff(pw, "description", mp.description, rp.get("description"))
                diff(pw, "ebgpMultihop", mp.ebgp_multihop, rp.get("ebgpMultihop"))
                af = rp.get("ipv4UnicastAddressFamily") or {}
                diff(pw, "rrClient", mp.rr_client, af.get("routeReflectorClient", False))
                caps = af.get("addressFamilyCapabilities", {})
                if af:
                    diff(pw, "sendCommunity", mp.send_community, caps.get("sendCommunity"))
            for k in set(mbp.peers) - {x.split("/")[0] if x.endswith("/32") else x for x in neigh}:
                print(f"{w} {k} extra peer")
                diffs += 1
    print(f"\n{checked} fields compared, {diffs} differences")
    return 1 if diffs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
