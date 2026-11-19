"""Batfish behaviours found by comparing the port with real Batfish on the 50-device test bed (2026-11-19).

Each test pins one rule, with the Batfish source that decides it. Configs are inline strings, so nothing is
written to disk.
"""
from bflite import questions as Q
from bflite.datamodel import Interface, ListWrapper, pd_str
from bflite.finalize import finalize_node
from bflite.loader import parse_config
from bflite.util import iface_name_key


def _nodes(*texts):
    nodes = {}
    for t in texts:
        n = parse_config(t)
        finalize_node(n)
        nodes[n.hostname] = n
    return nodes


def _iface_rows(nodes):
    return {str(r["Interface"]): r for r in Q.interface_properties(nodes).rows}


# --------------------------------------------------------------------------- answer cell text
def test_list_cells_print_like_pandas():
    # pybatfish ListWrapper -> pandas FrozenList -> pprint_thing (probed in pandas 3.0.5)
    assert str(ListWrapper(["a'b", "x\ty", 'q"r'])) == "['a\\'b', 'x\\ty', 'q\"r']"
    assert pd_str([Interface("a", "b"), Interface("a", "c d")]) == '[a[b], a["c d"]]'
    assert pd_str(["x", None, 1]) == "['x', None, 1]"
    assert pd_str(list(range(105))).endswith("98, 99, ...]")          # display.max_seq_items = 100


def test_interface_name_order():
    # InterfaceNameComparator: numbers numerically, text ignoring case
    names = ["Vlan10", "port-channel10", "Ethernet1/10", "Ethernet1/2"]
    assert sorted(names, key=iface_name_key) == ["Ethernet1/2", "Ethernet1/10", "port-channel10", "Vlan10"]


# --------------------------------------------------------------------------- IOS
IOS_HEAD = "hostname sw1\n!\n"


def test_ios_portfast_default_applies_to_later_switchports_only():
    cfg = IOS_HEAD + """spanning-tree portfast default
!
interface GigabitEthernet1/0/1
 switchport mode access
!
interface GigabitEthernet1/0/2
 switchport mode access
 spanning-tree portfast disable
!
interface Vlan10
 ip address 10.0.0.1 255.255.255.0
!
"""
    r = _iface_rows(_nodes(cfg))
    assert r["sw1[GigabitEthernet1/0/1]"]["Spanning_Tree_Portfast"] is True
    assert r["sw1[GigabitEthernet1/0/2]"]["Spanning_Tree_Portfast"] is False
    assert r["sw1[Vlan10]"]["Spanning_Tree_Portfast"] is False          # only copied for switchports


def test_ios_vlan_stanza_swallows_following_spanning_tree_lines():
    # Cisco_vlan.g4 vlan_spanning_tree_null: indentation is ignored, so the default never takes effect
    cfg = IOS_HEAD + """vlan 999
 name PARKING
!
spanning-tree portfast default
!
interface GigabitEthernet1/0/1
 switchport mode access
!
"""
    r = _iface_rows(_nodes(cfg))
    assert r["sw1[GigabitEthernet1/0/1]"]["Spanning_Tree_Portfast"] is False


def test_ios_vlan_lines_after_interface_set_encapsulation_vlan():
    # Cisco_interface.g4 if_vlan: 'vlan 20' ... 'vlan 210' become interface commands, last one wins
    cfg = IOS_HEAD + """interface TenGigabitEthernet1/1/8
 ip address 10.0.0.1 255.255.255.254
!
vlan 20
 name MGMT
!
vlan 210
 name USERS
!
interface TenGigabitEthernet1/1/1
 ip address 10.0.0.3 255.255.255.254
!
"""
    r = _iface_rows(_nodes(cfg))
    assert r["sw1[TenGigabitEthernet1/1/8]"]["Encapsulation_VLAN"] == 210
    assert r["sw1[TenGigabitEthernet1/1/1]"]["Encapsulation_VLAN"] is None


def test_ios_native_vlan_only_on_trunks_and_blacklisted_only_on_physical():
    cfg = IOS_HEAD + """interface GigabitEthernet1/0/1
 switchport mode access
!
interface GigabitEthernet1/0/2
 switchport mode trunk
!
interface Loopback0
 ip address 10.0.0.9 255.255.255.255
!
"""
    r = _iface_rows(_nodes(cfg))
    assert r["sw1[GigabitEthernet1/0/1]"]["Native_VLAN"] is None
    assert r["sw1[GigabitEthernet1/0/2]"]["Native_VLAN"] == 1
    assert r["sw1[GigabitEthernet1/0/1]"]["Blacklisted"] is False
    assert r["sw1[Loopback0]"]["Blacklisted"] is None
    assert "Interface_Type" not in r["sw1[Loopback0]"]                  # excluded unless asked for


# --------------------------------------------------------------------------- NX-OS
NXOS_L3VNI = """hostname leaf1
feature nv overlay
vlan 3001
  vn-segment 50001
vrf context TENANT1
  vni 50001
interface Vlan3001
  no shutdown
  vrf member TENANT1
  ip forward
interface loopback1
  ip address 10.0.0.1/32
interface nve1
  {shut}
  source-interface loopback1
  member vni 50001 associate-vrf
"""


def test_nxos_l3_vni_gets_tenant_interface():
    r = _iface_rows(_nodes(NXOS_L3VNI.format(shut="no shutdown")))["leaf1[nve~50001]"]
    assert (r["VRF"], r["Primary_Address"], r["Primary_Network"]) == ("TENANT1", "link-local:169.254.0.1", None)
    assert (r["Blacklisted"], r["Proxy_ARP"], r["Declared_Names"]) == (None, False, [])


def test_nxos_shut_nve_has_no_vnis():
    nodes = _nodes(NXOS_L3VNI.format(shut="shutdown"))
    assert "nve~50001" not in nodes["leaf1"].interfaces                # Nve starts shut down


# --------------------------------------------------------------------------- Junos
JUNOS = """set system host-name ex1
set groups JUMBO interfaces <*-0/0/*> mtu 9192
set apply-groups JUMBO
set vlans USERS vlan-id 10
set vlans USERS vxlan vni 10010
set vlans VOICE vlan-id 20
set interfaces ge-0/0/1 description "user port 1"
set interfaces ge-0/0/1 unit 0 family ethernet-switching vlan members USERS
set interfaces ge-0/0/2 unit 0 family ethernet-switching interface-mode trunk
set interfaces ge-0/0/2 unit 0 family ethernet-switching vlan members [ USERS VOICE ]
set interfaces irb unit 10 family inet address 10.1.0.1/24
set vlans USERS l3-interface irb.10
"""


def test_junos_units_inherit_description_and_mtu():
    r = _iface_rows(_nodes(JUNOS))["ex1[ge-0/0/1.0]"]
    assert (r["Description"], r["MTU"]) == ("user port 1", 9192)


def test_junos_set_line_with_brackets_is_dropped():
    # flat-Juniper grammar has no '[ ... ]' lists; Batfish reports the line and ignores it
    nodes = _nodes(JUNOS)
    r = _iface_rows(nodes)["ex1[ge-0/0/2.0]"]
    assert (r["Switchport_Mode"], r["Allowed_VLANs"]) == ("TRUNK", "")
    assert any("vlan members [ USERS VOICE ]" in w for w in nodes["ex1"].warnings)


def test_junos_no_irb_physical_and_no_vni_without_vtep_source():
    nodes = _nodes(JUNOS)
    assert "ex1[irb]" not in _iface_rows(nodes)
    v = {r["VLAN_ID"]: r for r in Q.switched_vlan_properties(nodes).rows}
    assert v[10]["VXLAN_VNI"] is None
    nodes = _nodes(JUNOS + "set interfaces lo0 unit 0 family inet address 10.9.9.9/32\n"
                           "set switch-options vtep-source-interface lo0.0\n")
    v = {r["VLAN_ID"]: r for r in Q.switched_vlan_properties(nodes).rows}
    assert v[10]["VXLAN_VNI"] == 10010


# =========================================================================== stress-test rules (2026-11-19)
# Found by the 1,000-config stress test (testbed/generate_stress.py); each confirmed in the Batfish source and
# with these exact configs in the batfish/allinone container.
from pathlib import Path  # noqa: E402

from bflite.ifnames import canonical  # noqa: E402
from bflite.loader import load_snapshot  # noqa: E402

EDGE = Path(__file__).parent / "edge_snapshot"


def _peer_rows(nodes):
    return {(r["Node"], r["Remote_IP"]): r for r in Q.bgp_peer_configuration(nodes).rows}


def test_ios_interface_name_table():
    # CiscoConfiguration.CISCO_INTERFACE_PREFIXES: first key starting with the typed prefix wins
    assert canonical("HundredGigE1/0/25", "CISCO_IOS") == "HundredGigabitEthernet1/0/25"
    assert canonical("TwentyFiveGigabitEthernet1/0/1", "CISCO_IOS") == "TwentyFiveGigE1/0/1"
    assert canonical("Gi0/1", "CISCO_IOS") == "GigabitEthernet0/1"
    assert canonical("Te1/1/1", "CISCO_IOS") == "TenGigabitEthernet1/1/1"
    cfg = IOS_HEAD + "interface HundredGigE1/0/25\n ip address 10.0.0.1 255.255.255.254\n!\n"
    r = _iface_rows(_nodes(cfg))["sw1[HundredGigabitEthernet1/0/25]"]
    assert r["Declared_Names"] == ["HundredGigE1/0/25"]               # only the name as written


def test_ios_vrrp_classic_only():
    cfg = IOS_HEAD + """interface Vlan10
 ip address 10.0.10.2 255.255.255.0
 vrrp 10 ip 10.0.10.1
!
interface Vlan20
 ip address 10.0.20.2 255.255.255.0
 vrrp 20 address-family ipv4
  address 10.0.20.1 primary
!
"""
    r = _iface_rows(_nodes(cfg))
    assert r["sw1[Vlan10]"]["VRRP_Groups"] == [10]
    assert r["sw1[Vlan20]"]["VRRP_Groups"] == []                     # IOS-XE syntax gives no group


def test_ios_secondary_addresses_sorted():
    cfg = IOS_HEAD + """interface GigabitEthernet0/1
 ip address 100.64.0.1 255.255.255.254
 ip address 10.9.9.1 255.255.255.252 secondary
!
"""
    r = _iface_rows(_nodes(cfg))["sw1[GigabitEthernet0/1]"]
    assert r["All_Prefixes"] == ["10.9.9.1/30", "100.64.0.1/31"]     # IP order, not primary first
    assert r["Primary_Address"] == "100.64.0.1/31"


IOS_BGP = """hostname r1
!
interface GigabitEthernet0/1
 ip address 10.9.0.0 255.255.255.254
!
interface GigabitEthernet0/2
 ip address 10.9.0.2 255.255.255.254
!
router bgp 65001
 no bgp default ipv4-unicast
 bgp listen range 10.8.0.0/24 peer-group DYN
 neighbor DYN peer-group
 neighbor DYN remote-as 65000 alternate-as 65001 65002
 neighbor 10.9.0.1 remote-as 65002
 neighbor 10.9.0.3 remote-as 65003
 !
 address-family ipv4
  no neighbor 10.9.0.3 activate
 exit-address-family
!
"""


def test_ios_bgp_default_ipv4_ignored_and_alternate_as():
    p = _peer_rows(_nodes(IOS_BGP))
    assert ("r1", "10.9.0.1") in p                                    # not activated, still an IPv4 peer
    assert ("r1", "10.9.0.3") not in p                                # 'no neighbor activate' drops it
    assert p[("r1", "10.8.0.0/24")]["Remote_AS"] == "65000-65002"


NX_HEAD = "!Command: show running-config\nversion 9.3(12) Bios:version 05.47\nhostname n1\n"


def test_nxos_vlan_1_not_implicit_and_access_vlan_sets_mode():
    cfg = NX_HEAD + """boot nxos bootflash:/nxos.9.3.12.bin
interface Ethernet1/1
  switchport
  switchport access vlan 1
  no shutdown
interface Ethernet1/2
  switchport access vlan 10
  no shutdown
interface Ethernet1/3
  no switchport
  ip address 10.7.0.0/31
  no shutdown
"""
    r = _iface_rows(_nodes(cfg))
    assert r["n1[Ethernet1/1]"]["Access_VLAN"] is None                # VLAN 1 is not defined
    assert r["n1[Ethernet1/2]"]["Switchport_Mode"] == "ACCESS"        # set by 'switchport access vlan'


def test_nxos_boot_image_with_sup_suffix_and_long_descriptions():
    cfg = """!Command: show running-config
version 6.2(24)
hostname agg1
boot kickstart bootflash:/n7000-s2-kickstart.6.2.24.bin sup-1
boot system bootflash:/n7000-s2-dk9.6.2.24.bin sup-1
feature bgp
interface Ethernet2/44
interface Ethernet2/45
  description """ + "x" * 255 + """
interface loopback0
  ip address 10.0.0.1/32
router bgp 65000
  neighbor 10.0.0.2
    remote-as 65001
    description """ + "y" * 81 + """
    address-family ipv4 unicast
"""
    nodes = _nodes(cfg)
    r = _iface_rows(nodes)
    assert r["agg1[Ethernet2/44]"]["Admin_Up"] is True               # N7K: L3 ports aren't shut by default
    assert r["agg1[Ethernet2/45]"]["Description"] is None             # over 254 characters
    assert _peer_rows(nodes)[("agg1", "10.0.0.2")]["Description"] is None   # over 80 characters


def test_junos_interface_range_physical_settings_only():
    cfg = """set system host-name ex1
set interfaces interface-range R member-range ge-0/0/0 to ge-0/0/2
set interfaces interface-range R description "range desc"
set interfaces interface-range R mtu 9000
set interfaces interface-range R unit 0 family ethernet-switching interface-mode access
set interfaces interface-range R2 member "ge-0/0/[5-6]"
set interfaces interface-range R2 ether-options 802.3ad ae0
set interfaces ge-0/0/1 description own
set interfaces ae0 unit 0 family inet address 10.0.0.1/24
"""
    r = _iface_rows(_nodes(cfg))
    assert (r["ex1[ge-0/0/0]"]["Description"], r["ex1[ge-0/0/0]"]["MTU"]) == ("range desc", 9000)
    assert r["ex1[ge-0/0/1]"]["Description"] == "own"                 # its own setting wins
    assert r["ex1[ge-0/0/6]"]["Channel_Group"] == "ae0"
    assert "ex1[ge-0/0/0.0]" not in r                                 # 'interface-range ... unit' is rejected


def test_junos_vrrp_zones_old_vlan_interface_and_export_dedupe():
    cfg = """set system host-name fw1
set vlans V10 vlan-id 10
set vlans V10 l3-interface vlan.10
set interfaces vlan unit 10 family inet address 10.1.10.2/24
set interfaces ge-0/0/1 unit 0 family ethernet-switching port-mode trunk
set interfaces ge-0/0/1 unit 0 family ethernet-switching vlan members V10
set interfaces irb unit 20 family inet address 10.1.20.2/24 vrrp-group 20 virtual-address 10.1.20.1
set interfaces xe-0/0/19 unit 0 family inet address 10.0.0.0/31
set security zones security-zone trust interfaces xe-0/0/19
set policy-options policy-statement P then accept
set routing-options autonomous-system 65001
set protocols bgp group G type external
set protocols bgp group G export P
set protocols bgp group G export P
set protocols bgp group G neighbor 10.0.0.1 peer-as 65002
"""
    nodes = _nodes(cfg)
    r = _iface_rows(nodes)
    assert r["fw1[irb.20]"]["VRRP_Groups"] == [20]
    # an irb unit that no VLAN's l3-interface points at is deactivated (found by probing this test's config)
    assert (r["fw1[irb.20]"]["Active"], r["fw1[irb.20]"]["Inactive_Reason"]) == (False, "Incomplete configuration")
    assert r["fw1[xe-0/0/19]"]["Zone_Name"] == "trust"                 # the interface the zone names
    assert r["fw1[xe-0/0/19.0]"]["Zone_Name"] is None
    v = {x["VLAN_ID"]: x for x in Q.switched_vlan_properties(nodes).rows}
    assert [str(i) for i in v[10]["Interfaces"]] == ["fw1[ge-0/0/1.0]"]   # vlan.10 gets no VLAN
    assert _peer_rows(nodes)[("fw1", "10.0.0.1")]["Export_Policy"] == ["P"]


def test_junos_without_local_address_is_established():
    jun = """set system host-name j1
set interfaces xe-0/0/0 unit 0 family inet address 10.5.0.1/31
set routing-options autonomous-system 65010
set protocols bgp group G type external
set protocols bgp group G neighbor 10.5.0.0 peer-as 65020
"""
    ios = """hostname c1
!
interface GigabitEthernet0/1
 ip address 10.5.0.0 255.255.255.254
!
router bgp 65020
 neighbor 10.5.0.1 remote-as 65010
!
"""
    nodes = _nodes(jun, ios)
    c = {r["Node"]: r for r in Q.bgp_session_compatibility(nodes).rows}
    s = {r["Node"]: r for r in Q.bgp_session_status(nodes).rows}
    assert c["j1"]["Configured_Status"] == "NO_LOCAL_IP"              # no inference without a dataplane
    assert (s["j1"]["Established_Status"], s["j1"]["Local_IP"]) == ("ESTABLISHED", "10.5.0.1")


def test_junos_description_quotes_by_format():
    from bflite.parsers.junos import flatten
    st = flatten('set interfaces ge-0/0/1 description "cust \\"ACME\\" link"\n')
    br = flatten('interfaces {\n    ge-0/0/1 {\n        description "cust \\"ACME\\" link";\n    }\n}\n')
    assert st[0][-1] == 'cust \\"ACME\\" link'                       # set: as written, outer quotes removed
    assert br[0][-1] == 'cust \\" ACME\\ " link'                     # brace: Batfish's flattener re-joins


def test_latin1_file_and_duplicate_hostnames():
    nodes = load_snapshot(EDGE)
    assert sorted(nodes) == ["sw9__configs__sw9-old.cfg", "sw9__configs__sw9.cfg"]
    r = _iface_rows(nodes)
    assert r["sw9__configs__sw9.cfg[GigabitEthernet0/1]"]["Description"] == "Café Zürich uplink"
