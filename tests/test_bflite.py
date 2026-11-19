"""Regression tests.

* test_example_* compare against answers produced by real Batfish for its own example network
  (tests/batfish_example, copied from github.com/batfish/batfish, Apache-2.0).
* test_sample_* pin Batfish semantics on the multi-vendor sample in tests/snapshot.
"""
import json
from pathlib import Path

import pytest

from bflite import Session, load_snapshot

HERE = Path(__file__).parent
EX = HERE / "batfish_example"


@pytest.fixture(scope="module")
def ex():
    bf = Session()
    bf.init_snapshot(str(EX))
    return bf


@pytest.fixture(scope="module")
def bf():
    s = Session()
    s.init_snapshot(str(HERE / "snapshot"))
    return s


def _find_rows(o):
    if isinstance(o, dict):
        if "rows" in o:
            return o["rows"]
        for v in o.values():
            r = _find_rows(v)
            if r is not None:
                return r
    if isinstance(o, list):
        for v in o:
            r = _find_rows(v)
            if r is not None:
                return r
    return None


# ------------------------------------------------------------------ Batfish reference answers
def test_example_vimodel_matches_batfish():
    ours = load_snapshot(EX)
    ref = json.load(open(EX / "viModel.ref"))[0]["nodes"]
    for host, rn in ref.items():
        if host.startswith("host"):
            continue  # host json files, not device configs
        node = ours[host]
        assert set(node.interfaces) == set(rn["interfaces"]), host
        for name, ri in rn["interfaces"].items():
            mi = node.interfaces[name]
            assert [str(a) for a in mi.addresses] == (ri.get("allPrefixes") or []), (host, name)
            assert mi.bandwidth == ri["bandwidth"], (host, name)
            assert mi.speed == ri.get("speed"), (host, name)
            assert bool(mi.admin_up) == ri["adminUp"], (host, name)
            assert mi.switchport_mode == ri["switchportMode"], (host, name)
            assert bool(mi.proxy_arp) == ri["proxyArp"], (host, name)
        for vrf, rv in rn["vrfs"].items():
            rbp = rv.get("bgpProcess")
            if not rbp:
                continue
            mbp = node.bgp[vrf]
            assert mbp.router_id == rbp["routerId"]
            assert mbp.multipath_ebgp == rbp["multipathEbgp"]
            assert set(mbp.peers) == set(rbp.get("neighbors", {}))
            for k, rp in rbp["neighbors"].items():
                p = mbp.peers[k]
                assert p.resolved_local_ip == rp.get("localIp"), (host, k)
                assert p.remote_as == rp["remoteAsns"], (host, k)
                assert p.peer_group == rp.get("group"), (host, k)


def test_example_session_status_matches_batfish(ex):
    ref = _find_rows(json.load(open(EX / "bgpSessionStatus.ref")))
    df = ex.q.bgpSessionStatus(nodes="as2.*", remoteNodes="as3.*").answer().frame()
    assert len(df) == len(ref)
    for r, (_, mine) in zip(ref, df.iterrows()):
        for col in ("Node", "Remote_Node"):
            assert mine[col] == r[col]["name"]
        for col in ("VRF", "Local_IP", "Remote_AS", "Session_Type", "Established_Status", "Local_AS"):
            assert mine[col] == r[col], col
        assert mine["Remote_IP"] == r["Remote_IP"]["value"]
        assert list(mine["Address_Families"]) == r["Address_Families"]


def test_example_doc_rows(ex):
    """Rows shown in the pybatfish documentation notebooks for this network."""
    c = ex.q.bgpSessionCompatibility(nodes="as1border1").answer().frame().set_index("Remote_IP")
    assert c.loc["3.2.2.2", "Configured_Status"] == "NO_LOCAL_IP"
    assert c.loc["3.2.2.2", "Session_Type"] == "EBGP_SINGLEHOP"
    assert c.loc["1.10.1.1", "Configured_Status"] == "UNIQUE_MATCH"
    assert c.loc["1.10.1.1", "Local_IP"] == "1.1.1.1"
    assert c.loc["1.10.1.1", "Remote_Node"] == "as1core1"
    p = ex.q.bgpPeerConfiguration(nodes="as1border1").answer().frame().set_index("Remote_IP")
    assert p.loc["10.12.11.2", "Import_Policy"] == ["as2_to_as1"]
    assert p.loc["10.12.11.2", "Export_Policy"] == ["as1_to_as2"]
    assert p.loc["10.12.11.2", "Local_IP"] == "10.12.11.1"
    assert p.loc["3.2.2.2", "Peer_Group"] == "bad-ebgp"
    e = ex.q.layer3Edges().answer().frame()
    first = [(str(r.Interface), list(r.IPs), str(r.Remote_Interface), list(r.Remote_IPs)) for r in e.head(5).itertuples()]
    assert first == [
        ("as1border1[GigabitEthernet0/0]", ["1.0.1.1"], "as1core1[GigabitEthernet1/0]", ["1.0.1.2"]),
        ("as1border1[GigabitEthernet1/0]", ["10.12.11.1"], "as2border1[GigabitEthernet0/0]", ["10.12.11.2"]),
        ("as1border2[GigabitEthernet0/0]", ["10.13.22.1"], "as3border2[GigabitEthernet0/0]", ["10.13.22.3"]),
        ("as1border2[GigabitEthernet1/0]", ["1.0.2.1"], "as1core1[GigabitEthernet0/0]", ["1.0.2.2"]),
        ("as1core1[GigabitEthernet0/0]", ["1.0.2.2"], "as1border2[GigabitEthernet1/0]", ["1.0.2.1"]),
    ]
    i = ex.q.interfaceProperties(nodes="as1border1", interfaces="Ethernet0/0").answer().frame().iloc[0]
    assert i["Inactive_Reason"] == "Administratively down" and i["Bandwidth"] == 10000000.0
    assert i["Allowed_VLANs"] == "" and i["Switchport_Trunk_Encapsulation"] == "DOT1Q" and i["Proxy_ARP"] is True


# ------------------------------------------------------------------ multi-vendor sample
def _row(df, **kv):
    m = df
    for k, v in kv.items():
        m = m[m[k].astype(str) == str(v)]
    assert len(m) == 1, kv
    return m.iloc[0]


def test_sample_frame_types(bf):
    df = bf.q.bgpPeerConfiguration().answer().frame()
    assert all(str(t) == "object" for t in df.dtypes)
    assert df.isna().sum().sum() == df.isnull().sum().sum()


def test_sample_nxos_template_and_evpn(bf):
    p = bf.q.bgpPeerConfiguration(nodes="spine1").answer().frame()
    r = _row(p, Remote_IP="10.1.0.11")
    assert r.Peer_Group == "LEAFS" and r.Route_Reflector_Client and r.Cluster_ID == "10.1.0.1"
    assert r.Local_IP == "10.1.0.1" and r.Send_Community
    s = bf.q.bgpSessionCompatibility(nodes="leaf1").answer().frame()
    r = _row(s, Remote_IP="10.1.0.1")
    assert list(r.Address_Families) == ["EVPN"] and r.Configured_Status == "UNIQUE_MATCH"


def test_sample_nxos_vrf_router_id_and_dynamic(bf):
    pc = bf.q.bgpProcessConfiguration(nodes="leaf1").answer().frame()
    assert _row(pc, VRF="TENANT1").Router_ID == "172.16.10.1"      # lowest IP in VRF (no loopback)
    s = bf.q.bgpSessionCompatibility(nodes="leaf1").answer().frame()
    r = _row(s, Remote_IP="192.168.200.0/24")
    assert r.Configured_Status == "NO_MATCH_FOUND" and r.Session_Type == "UNSET"


def test_sample_ios_peer_group_and_shutdown(bf):
    p = bf.q.bgpPeerConfiguration(nodes="border1").answer().frame()
    assert "198.18.0.1" not in set(p.Remote_IP)                   # shutdown neighbor dropped
    r = _row(p, Remote_IP="203.0.113.2")
    assert r.Peer_Group == "ISP" and r.Remote_AS == "65300" and r.Description == "upstream"
    assert list(r.Import_Policy) == ["RM-FROM-ISP"] and r.Send_Community


def test_sample_cross_vendor_sessions(bf):
    s = bf.q.bgpSessionCompatibility().answer().frame()
    assert _row(s, Node="border1", Remote_IP="192.168.100.0").Remote_Node == "leaf1"
    assert _row(s, Node="mx1", Remote_IP="203.0.113.1").Remote_Node == "border1"
    r = _row(s, Node="ex1", Remote_IP="10.5.5.1")
    assert list(r.Address_Families) == ["IPV4_UNICAST", "EVPN"]
    st = bf.q.bgpSessionStatus().answer().frame()
    assert set(st.Established_Status) == {"ESTABLISHED", "NOT_ESTABLISHED"}


def test_sample_junos(bf):
    pc = bf.q.bgpProcessConfiguration(nodes="mx1").answer().frame()
    d = _row(pc, VRF="default")
    assert d.Tie_Breaker == "ROUTER_ID" and d.Multipath_EBGP and d.Multipath_Match_Mode == "FIRST_AS"
    assert "192.0.2.99" not in d.Neighbors                          # inactive: group skipped
    p = bf.q.bgpPeerConfiguration(nodes="mx1").answer().frame()
    assert list(_row(p, Remote_IP="203.0.113.1").Import_Policy) == ["FROM-BORDER"]
    assert _row(p, Remote_IP="100.64.0.0/24").Is_Passive
    i = bf.q.interfaceProperties(nodes="mx1").answer().frame()
    assert _row(i, Interface="mx1[ge-0/0/0]").MTU == 9192           # from apply-groups <ge-*>
    assert _row(i, Interface="mx1[ge-0/0/2.0]").Inactive_Reason == "Parent interface is down"
    assert _row(i, Interface="mx1[fxp0.0]").Inactive_Reason == "Ignored management interface"


def test_sample_l2(bf):
    i = bf.q.interfaceProperties().answer().frame()
    assert _row(i, Interface="leaf2[Vlan10]").Inactive_Reason == "Autostate failure"
    assert _row(i, Interface="spine1[Ethernet1/3]").Admin_Up is False   # N9K L3 default shutdown
    pc = _row(i, Interface="leaf1[port-channel10]")
    assert list(pc.Channel_Group_Members) == ["Ethernet1/1"] and pc.Allowed_VLANs == "10,20"
    ae = _row(i, Interface="ex1[ae0]")
    assert ae.Bandwidth == 1e9                                       # only active members count
    v = bf.q.switchedVlanProperties(nodes="leaf1").answer().frame()
    r = _row(v, VLAN_ID=10)
    assert r.VXLAN_VNI == 10010
    # InterfaceNameComparator: case-insensitive, numbers compared numerically (matches the test-bed reference)
    assert [str(x) for x in r.Interfaces] == ["leaf1[Ethernet1/1]", "leaf1[Ethernet1/2]", "leaf1[port-channel10]",
                                              "leaf1[Vlan10]"]
    e = bf.q.layer3Edges().answer().frame()
    pairs = {(str(a), str(b)) for a, b in zip(e.Interface, e.Remote_Interface)}
    assert ("leaf1[Vlan10]", "leaf2[Vlan10]") not in pairs          # same anycast IP -> no edge
    assert ("border1[GigabitEthernet0/0/1.100]", "leaf1[Ethernet1/3.100]") in pairs
