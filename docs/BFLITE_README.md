# bflite: Batfish's config questions in pure Python

`bflite` parses Cisco IOS/IOS-XE, Cisco NX-OS and Juniper Junos configs. It answers the
pybatfish questions used for BGP, L3 and L2 extraction, with the **same column names,
column order and cell types** as `pybatfish`. You don't need the Batfish Docker image,
Java or a server.

It's a semantic port, not a line-by-line translation. Batfish's ANTLR grammars and Java
converters (about 1M lines) were read for the rules that decide each output value:
defaults, inference, inheritance and deactivation. Those rules are reimplemented here.
The Batfish class that each rule comes from is named in the code's comments.

<!-- toc -->
**Contents**

- [Drop-in usage](#drop-in-usage)
- [Questions implemented](#questions-implemented)
- [How it was verified](#how-it-was-verified)
- [Batfish behaviour reproduced](#batfish-behaviour-reproduced)
- [Known gaps and approximations (read before trusting in production)](#known-gaps-and-approximations-read-before-trusting-in-production)
- [Layout](#layout)
- [Licence note](#licence-note)
<!-- /toc -->

## Drop-in usage

```python
# from pybatfish.client.session import Session      # before
from bflite import Session                           # after

bf = Session(host="ignored")
bf.set_network("dc")
bf.init_snapshot("snapshots/2026-11-18", name="today", overwrite=True)   # dir with configs/

bf.q.bgpPeerConfiguration(nodes="/leaf.*/").answer().frame()
bf.q.bgpSessionCompatibility(status="HALF_OPEN|NO_MATCH_FOUND").answer().frame()
bf.q.interfaceProperties(properties="Active|VRF|Primary_Address").answer().frame()
bf.q.switchedVlanProperties(vlans="10-20").answer().frame()
```

From the command line:

```
python -m bflite <snapshot_dir> bgpSessionCompatibility --nodes leaf1 --out sessions.csv
```

The frame matches pybatfish's `_rows_to_frame`. Every column is `dtype=object`, nulls are
`None` (not `NaN`), and list or set cells are list subclasses. `Interface` cells have
`.hostname`, `.interface`, and the same `str()` (`leaf1[Ethernet1/1]`) and `repr()`.

Requirements: Python 3.9+ and pandas. There are no other dependencies.

[Back to top](#bflite-batfishs-config-questions-in-pure-python)

## Questions implemented

| Question | Parameters supported |
|---|---|
| `bgpProcessConfiguration` | nodes, properties |
| `bgpPeerConfiguration` | nodes, properties |
| `bgpSessionCompatibility` | nodes, remoteNodes, status, type |
| `bgpSessionStatus` | nodes, remoteNodes, status, type |
| `interfaceProperties` | nodes, interfaces, properties, excludeShutInterfaces |
| `ipOwners` | duplicatesOnly, ips |
| `layer3Edges` | nodes, remoteNodes |
| `switchedVlanProperties` | nodes, interfaces, vlans, excludeShutInterfaces |

Node specifiers support names, `/regex/` and comma-separated unions. A bare regex also
works, as in older pybatfish.

[Back to top](#bflite-batfishs-config-questions-in-pure-python)

## How it was verified

* **Against real Batfish output.** Batfish ships a model and answers for its own example
  network (`tests/batfish_example`, Apache-2.0). bflite matches all **1,388**
  interface and BGP fields in that model. It also matches the `bgpSessionStatus`
  reference answer row for row, and the rows shown in the pybatfish documentation.
* **Against Batfish's own unit-test assertions** for NX-OS: default shutdown and
  switchport inference, and switchport conversion.
* **Robustness.** All 834 vendor test configs from Batfish's test suite parse without
  errors (148 NX-OS, 172 IOS, 514 Junos).
* **A multi-vendor sample** (`tests/snapshot`) pins down cross-vendor behaviour.
  It includes an NX-OS VXLAN/EVPN fabric with templates, VRFs, vPC, anycast SVIs and
  NVE. It also has an IOS-XE border with peer-groups and VRF sub-interfaces, a Junos MX
  with apply-groups, inactive groups and a VRF with dynamic peers, and a Junos EX with
  ae0, irb and VLAN names.

Run them all with `python -m pytest tests`.

`tools/compare_vimodel.py <snapshot> <viModel.json>` diffs bflite against any Batfish
`viModel` dump. If you can capture `bf.q.viModel().answer()` from any Batfish anywhere
(for example a colleague's laptop or an approved CI runner), run this tool on your real
configs to find the gaps.

[Back to top](#bflite-batfishs-config-questions-in-pure-python)

## Batfish behaviour reproduced

* **Hostnames** are lower-cased. Interface names are canonicalised (`Gi0/1` becomes
  `GigabitEthernet0/1`, `po10` becomes `port-channel10`), and the names as typed are
  kept in `Declared_Names`.
* **NX-OS defaults** are inferred from the platform, version and the config itself,
  including the L2-or-L3 default mode and the L3 default shutdown (`CiscoNxosPreprocessor`).
  Trunk allowed VLANs are intersected with the defined VLANs. An access VLAN that isn't
  defined becomes `None`.
* **Interfaces are deactivated** in Batfish's order: admin down, then management
  interfaces (`mgmt*`, `fxp0`, `em0` and VRF `mgmt*`/`management`), then port-channels or
  `ae` with no members or no active members, then a sub-interface or unit whose parent
  is down, then SVI or irb autostate. The `Inactive_Reason` strings are Batfish's own.
  Aggregate bandwidth is the sum of the active members.
* **BGP:**
  * IOS: peer-group inheritance, `activate` and `no bgp default ipv4-unicast`, and
    shutdown or remote-as-less peers dropped. Local IP comes from `update-source`, else
    the last connected match.
  * NX-OS: `template peer` inheritance, including per-AF settings. Peers with no address
    family are dropped. The local IP is the first connected match.
  * Junos: group and neighbor merging, `type internal`, `allow` gives dynamic peers, and
    the local IP is `local-address` only.
  * Router IDs are inferred with each vendor's own rule.
  * `Multipath_*`, `Tie_Breaker` and `Multipath_Match_Mode` follow each vendor's
    conversion. Junos defaults to `FIRST_AS`, and IOS `maximum-paths N` sets both eBGP
    and iBGP.
* **Session compatibility** is a port of `BgpTopologyUtils` and
  `BgpSessionCompatibilityAnswerer`: AS-pair checks including confederations,
  feasible local IPs, the rule that a peer can't peer with its own node/VRF,
  `HALF_OPEN`/`MULTIPLE_REMOTES`, and `DYNAMIC_MATCH` rows for passive peers.
* **Junos** configs can be hierarchical or `set` format. `inactive:` statements and
  `deactivate` lines are skipped, and `groups`/`apply-groups` are expanded, including
  `<*>` wildcards.

[Back to top](#bflite-batfishs-config-questions-in-pure-python)

## Known gaps and approximations (read before trusting in production)

1. **`bgpSessionStatus` doesn't run a dataplane.** Batfish runs traceroutes over the
   computed FIB, including ACLs. bflite uses a stand-in:
   * Single-hop eBGP counts as established when both IPs sit on the two ends of a
     synthesized L3 edge.
   * iBGP and multihop count as established when the two node/VRF endpoints are
     connected through L3 edges.

   Treat `ESTABLISHED` as "plausibly established". `NOT_COMPATIBLE` matches Batfish
   exactly.
2. **No FIB-based source-IP inference.** A peer with no local IP that Batfish would
   resolve through routing gets no edges from its own side.
3. **No layer-1 topology file**, so `layer3Edges` behaves as Batfish does without
   `layer1_topology.json`.
4. **Unnumbered BGP peers** (`neighbor Ethernet1/1` on NX-OS) are reported, but not
   matched to their remote ends.
5. **IPv6 peers and addresses aren't modelled.** Batfish's IPv4 questions ignore them
   too.
6. **Not modelled:** HSRP/VRRP virtual IPs in IP ownership, VRRP/HSRP detail columns,
   Junos `interface-range`, Junos logical-systems, and IOS `interface range`.
7. **Row order differs.** Batfish doesn't guarantee one either, so sort before
   comparing. `Neighbors` in `bgpProcessConfiguration` is in config order.
8. **The `interfaceProperties` columns** follow current Batfish, which includes
   `Interface_Type`. If your pybatfish or Batfish version predates that column, drop it.

[Back to top](#bflite-batfishs-config-questions-in-pure-python)

## Layout

```
bflite/
  parsers/tree.py     indentation tree for IOS/NX-OS (banners, cert blobs handled)
  parsers/cisco.py    IOS + NX-OS extraction and conversion (incl. NX-OS preprocessor)
  parsers/junos.py    flattener, apply-groups, interfaces/VLANs/BGP
  model.py            vendor-independent model
  finalize.py         post-processing (deactivation, autostate, bandwidth, router-id, local IP)
  questions.py        the eight questions
  session.py          pybatfish-style Session / bf.q / answer().frame()
tools/                compare_vimodel.py, fuzz_parse.py
tests/                pytest suite, Batfish example network + reference answers, sample snapshot
```

[Back to top](#bflite-batfishs-config-questions-in-pure-python)

## Licence note

Batfish is Apache-2.0. bflite reimplements its behaviour, and the example network and
reference answers under `tests/batfish_example` are copied from the Batfish repo, so keep
the attribution if you redistribute.

[Back to top](#bflite-batfishs-config-questions-in-pure-python)
