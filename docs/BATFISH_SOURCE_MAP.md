# Batfish source analysis: what produces the BGP / L3 / L2 data, and how to extract it

Repo analysed: `github.com/batfish/batfish` (master, Nov 2026). All paths are relative to `projects/`.
Line numbers are approximate; they drift between commits.

<!-- toc -->
**Contents**

- [1. How Batfish produces an answer row](#1-how-batfish-produces-an-answer-row)
- [2. Stage 2: grammars (syntax)](#2-stage-2-grammars-syntax)
- [3. Stage 3: extractors (parse tree to vendor model)](#3-stage-3-extractors-parse-tree-to-vendor-model)
- [4. Stage 4: conversion. This is where the data is decided.](#4-stage-4-conversion-this-is-where-the-data-is-decided)
  - [IOS / IOS-XE: batfish/.../representation/cisco/CiscoConfiguration.java (3,985 lines)](#ios--ios-xe-batfishrepresentationciscociscoconfigurationjava-3985-lines)
  - [NX-OS: batfish/.../vendor/cisco_nxos/representation/](#nx-os-batfishvendorcisco_nxosrepresentation)
  - [Junos: batfish/.../representation/juniper/JuniperConfiguration.java (5,376 lines)](#junos-batfishrepresentationjuniperjuniperconfigurationjava-5376-lines)
- [5. Stage 5: post-processing. Small and must be exact.](#5-stage-5-post-processing-small-and-must-be-exact)
- [6. Stage 6: topology and answerers](#6-stage-6-topology-and-answerers)
- [7. Extraction options](#7-extraction-options)
  - [Option A: semantic port with hand-written parsers (what bflite does now)](#option-a-semantic-port-with-hand-written-parsers-what-bflite-does-now)
  - [Option B: generate Python parsers from Batfish's grammars, then write slim extractors](#option-b-generate-python-parsers-from-batfishs-grammars-then-write-slim-extractors)
  - [Option C: run the Batfish JVM without Docker](#option-c-run-the-batfish-jvm-without-docker)
  - [Recommendation](#recommendation)
- [8. Mapping to bflite modules](#8-mapping-to-bflite-modules)
<!-- /toc -->

## 1. How Batfish produces an answer row

Every value in a pybatfish frame passes through six stages. Only stages 4 to 6 decide the values.

| # | Stage | What it does | Size (all vendors and features) | Part we need |
|---|---|---|---|---|
| 1 | Format detection | Picks the vendor grammar for each file | small | all |
| 2 | Lex and parse (ANTLR `.g4`) | Text to parse tree | ~63k lines of grammar for the 3 vendors | ~10% |
| 3 | Extract (`*ControlPlaneExtractor`, `ConfigurationBuilder`) | Parse tree to a vendor-specific model | 32k lines | ~20% (see section 3) |
| 4 | Convert (`CiscoConfiguration`, `Conversions`, `JuniperConfiguration`) | Vendor model to the vendor-independent model; **defaults, inheritance, inference** | 16.6k lines | ~3k lines |
| 5 | Post-process (`Batfish.java`, `InterfaceDependencies`) | Deactivation, autostate, aggregate bandwidth | ~400 lines | all of it |
| 6 | Topology and answerers (`BgpTopologyUtils`, `TopologyUtil`, `question/*`) | Session matching, L3 edges, column building | ~2.5k lines | all of it |

**Key finding:** the grammars (stage 2) are the bulk of the code, but almost every subtle output
difference comes from stages 4 to 6. That's where the defaults, "last match wins" rules and
deactivation order live. A perfect parser plus naive conversion gives *wrong* answers; a simple
parser plus faithful conversion gives the *right* answers for `show run` output.

[Back to top](#batfish-source-analysis-what-produces-the-bgp--l3--l2-data-and-how-to-extract-it)

## 2. Stage 2: grammars (syntax)

| Vendor | Entry grammar | Files we need | Lines |
|---|---|---|---|
| IOS/IOS-XE | `batfish/src/main/antlr4/org/batfish/grammar/cisco/CiscoParser.g4` | `Cisco_interface.g4` (2,060), `Cisco_bgp.g4` (1,019), `Cisco_vlan.g4` (209), `Cisco_common.g4` (743), `CiscoLexer.g4` (8,413) | ~12.4k |
| NX-OS | `.../vendor/cisco_nxos/grammar/CiscoNxosParser.g4` | `CiscoNxos_interface.g4` (2,043), `CiscoNxos_bgp.g4` (1,182), `CiscoNxos_common.g4` (639), `CiscoNxosLexer.g4` (3,901) | ~7.8k |
| Junos | `.../grammar/flatjuniper/FlatJuniperParser.g4` (plus `grammar/juniper/JuniperParser.g4` for the brace format) | `FlatJuniper_interfaces.g4` (1,246), `FlatJuniper_bgp.g4` (904), `FlatJuniper_routing_instances.g4` (1,602), `FlatJuniper_common.g4` (793), `FlatJuniperLexer.g4` (6,231) | ~10.8k |

**Portability check (relevant to extraction):**
- **Parser grammars** contain almost no Java. There is one semantic predicate in total
  (`{isVlanId($v)}?` in the Cisco grammar), and the NX-OS and Junos parsers have none. They
  can be generated for ANTLR's **Python target** unchanged, apart from that predicate.
- **The lexers** depend on small Java base classes. Each needs a Python version:
  - `CiscoBaseLexer`: `lastTokenType()`, `bannerIosDelimiterFollows()`, `less()` (12 actions)
  - `CiscoNxosBaseLexer`: `lastTokenType()`, `bannerDelimiterFollows()` (2 actions)
  - `FlatJuniperBaseLexer`: `lastTokenType()`, `setWildcard()`, `less()` (31 actions)
- **The lexers can't be sliced**, because they are mode-based and shared. You generate the full
  lexer even if you only use interface and BGP rules.

[Back to top](#batfish-source-analysis-what-produces-the-bgp--l3--l2-data-and-how-to-extract-it)

## 3. Stage 3: extractors (parse tree to vendor model)

| File | Lines | Listener methods | Relevant to us |
|---|---|---|---|
| `batfish/.../grammar/cisco/CiscoControlPlaneExtractor.java` | 11,076 | 663 | ~153 (`If_*`, `Router_bgp_*`, neighbor/peer-group, `S_vlan*`, VRF, HSRP/VRRP) |
| `batfish/.../vendor/cisco_nxos/grammar/CiscoNxosControlPlaneExtractor.java` | 8,381 | 453 | ~148 (`I_*`, `Rb_*`, vlan/vn-segment, `Vc_*`, nve, hsrp) |
| `batfish/.../vendor/cisco_nxos/grammar/CiscoNxosPreprocessor.java` | 557 | – | **all**: platform, L2/L3 default and L3 shutdown inference |
| `batfish/.../grammar/flatjuniper/ConfigurationBuilder.java` | 12,973 | 991 | ~138 (`I_*`/`Ifi*`/`Ife*`, `B_*`, `Ri_*`, `S_vlans*`, `Ro_*`) |
| `batfish/.../grammar/juniper/JuniperFlattener.java` | 251 | – | all: brace format to `set` lines |
| `batfish/.../grammar/flatjuniper/{Hierarchy, GroupTreeBuilder, GroupInheritor, ApplyGroupsMarker, DeactivatedLinePruner, InsertDeleteApplicator, ApplyPathApplicator}` | ~2.5k | – | all: groups/apply-groups, deactivate, insert/delete |

Anchor methods, if you read further:
`CiscoControlPlaneExtractor.enterRouter_bgp_stanza` (~3236), `exitIf_ip_address` (~5465),
`exitIf_switchport_mode` (~5831); `CiscoNxosControlPlaneExtractor.enterRb_neighbor` (~4303),
`exitI_ip_address_concrete` (~5919), `exitI_switchport_mode_*` (~6175);
`ConfigurationBuilder.enterB_group/enterB_neighbor` (~3472), `exitRi_interface` (~9094),
`exitS_vlans_named` (~10148).

[Back to top](#batfish-source-analysis-what-produces-the-bgp--l3--l2-data-and-how-to-extract-it)

## 4. Stage 4: conversion. This is where the data is decided.

### IOS / IOS-XE: `batfish/.../representation/cisco/CiscoConfiguration.java` (3,985 lines)
| Lines | Method | Decides |
|---|---|---|
| ~649 | `getBgpRouterId` | Router_ID: process, then default VRF, then highest Loopback, then highest IP |
| ~913 | `getUpdateSource` | Local_IP: update-source, else the **last** interface subnet that contains the peer |
| ~1050–1290 | `toBgpProcess` + peer loop | Drops inactive/shutdown/no-remote-as peers; `maximum-paths N` sets **both** eBGP and iBGP; multipath-relax gives PATH_LENGTH; peer-group inheritance (`BgpPeerGroup.inheritUnsetFields`) |
| ~1399 | `computeCiscoInterfaceType` | Interface_Type |
| ~1485–1700 | `toInterface` | Admin_Up, addresses only when not switchport, Switchport_Mode, Access/Native/Allowed VLANs, bandwidth = bandwidth or speed or name default, proxy-arp default true, filters only if the ACL exists |
| `representation/cisco/Interface.java` | `getDefaultSpeed/Bandwidth` | Speed and Bandwidth defaults by name |

### NX-OS: `batfish/.../vendor/cisco_nxos/representation/`
| File:lines | Method | Decides |
|---|---|---|
| `Conversions.java` ~128–215 | `getBgpRouterId`, `inferRouterId` | VRF router-id, else loopback0, else lowest loopback, else lowest IP. No fallback to the default VRF |
| `Conversions.java` ~338 | `computeUpdateSource` | Local_IP: update-source, else the **first** subnet match; none for dynamic peers |
| `Conversions.java` ~397–600 | `toBgpNeighbor` | Template inheritance, active only if the neighbor has an address family, send-community standard only, cluster-id falls back to router-id, Peer_Group = inherited template |
| `CiscoNxosConfiguration.java` ~525–600 | `convertBgp`, `convertBgpVrf` | Per-VRF processes, multipath, tie-breaker |
| `CiscoNxosConfiguration.java` ~1189, ~2145–2400 | `convertInterfaces`, `toInterface` | Default shutdown per type and mode, trunk VLANs ∩ defined VLANs, undefined access VLAN becomes None, Ethernet 1G default, port-channel bandwidth from members |
| `CiscoNxosConfiguration.java` ~1523–1560 | `convertVrfs`, `convertNves`, `convertNveVni` | VRF shutdown, VXLAN_VNI (requires an NVE member VNI) |

### Junos: `batfish/.../representation/juniper/JuniperConfiguration.java` (5,376 lines)
| Lines | Method | Decides |
|---|---|---|
| ~498–965 | `createBgpProcess` | Group/neighbor merge, `type internal` remote AS = local AS, `allow` gives passive peers (any-AS default), Local_IP = **only** `local-address`, FIRST_AS multipath default, `external-router-id` gives ROUTER_ID, `cluster` gives RR client, IPv4 AF always on |
| ~1996, ~2038 | `toInterfaceNonUnit`, `toInterface` | Physical vs unit interfaces, ethernet-switching modes, IRB VLAN, `disable` |
| ~2271 | `computeAccessVlan` | Access VLAN from VLAN names, numbers or ranges |
| ~5327 | `getRouterId` | RI router-id, else lowest `lo*` IP, else lowest IP; default RI falls back to lo0.0 |

[Back to top](#batfish-source-analysis-what-produces-the-bgp--l3--l2-data-and-how-to-extract-it)

## 5. Stage 5: post-processing. Small and must be exact.

| File:lines | Method | Effect on output |
|---|---|---|
| `Batfish.java` ~1879 | `updateBlacklistedAndInactiveConfigs` | Fixes the **order**: management, then dependencies, then autostate |
| `Batfish.java` ~1731 | `processManagementInterfaces` | `Ignored management interface` (regexes for mgmt/fxp0/em0, VRF mgmt*/management) |
| `common/.../interface_dependency/InterfaceDependencies.java` | `getInterfacesToDeactivate` | `No configured member interfaces`, `No operational member interfaces`, `Parent interface is down` |
| `Batfish.java` ~803 | `disableUnusableVlanInterfaces` | `Autostate failure` for SVI/IRB with no active member |
| `Batfish.java` ~572 | `computeAggregatedInterfaceBandwidths` | Port-channel/ae bandwidth = sum of **active** members |

[Back to top](#batfish-source-analysis-what-produces-the-bgp--l3--l2-data-and-how-to-extract-it)

## 6. Stage 6: topology and answerers

| File:lines | Produces |
|---|---|
| `common/.../bgp/BgpTopologyUtils.java` ~71–545, ~692 | Configured BGP edges, AS-pair and confederation rules (`computeAsPair`), candidate sanity checks |
| `common/.../datamodel/BgpSessionProperties.java` | Session_Type, Address_Families intersection |
| `question/.../bgpsessionstatus/BgpSessionAnswererUtils.java` | Configured_Status precedence (`NO_LOCAL_IP` … `HALF_OPEN`, `MULTIPLE_REMOTES`) |
| `question/.../bgpsessionstatus/BgpSessionCompatibilityAnswerer.java`, `BgpSessionStatusAnswerer.java` | Rows for active and passive peers |
| `question/.../bgpproperties/*` and `BgpPeerPropertySpecifier`, `BgpProcessPropertySpecifier` | Column getters (Is_Passive = dynamic peer, Send_Community from IPv4 then EVPN, Cluster_ID only for RR clients) |
| `common/.../topology/TopologyUtil.java` ~452, ~576 | `layer3Edges` subnet-bucket synthesis |
| `question/.../switchedvlanproperties/SwitchedVlanPropertiesAnswerer.java` | VLAN to interfaces, VNI-only rows |
| `question/.../InterfacePropertySpecifier.java`, `ipowners/IpOwnersAnswerer.java` | Interface columns (sorted alphabetically), IP ownership |
| `pybatfish/datamodel/answer/table.py` (pybatfish repo) | Frame shape: object dtype, None, ListWrapper, Interface objects |

[Back to top](#batfish-source-analysis-what-produces-the-bgp--l3--l2-data-and-how-to-extract-it)

## 7. Extraction options

### Option A: semantic port with hand-written parsers (what bflite does now)
- Port stages 4 to 6 faithfully (~6k Java lines, now ~2.8k Python lines). Replace stages 2 and 3
  with an indentation-tree parser (Cisco) and a flattener plus `set` parser (Junos).
- **Pros:** small, fast and readable. Pure Python. The conversion rules are where fidelity comes from.
- **Cons:** syntax edge cases are found one at a time (odd `show run` variants, rare keywords).

### Option B: generate Python parsers from Batfish's grammars, then write slim extractors
- Run the ANTLR tool once, on any machine with Java (build time only, not at runtime), with
  `-Dlanguage=Python3` on the three parser grammars. Port the three small base-lexer classes
  and the single predicate (section 2).
- Write Python listeners for only the ~150 relevant rules per vendor (section 3). Feed them into
  bflite's existing model, `finalize.py` and `questions.py`.
- **Pros:** Batfish-grade syntax coverage, including banners, odd abbreviations and every keyword variant.
- **Cons:** the generated lexer and parser code is large. The Python ANTLR runtime is about 10–50x
  slower than Java, so a big IOS config can take seconds. You also depend on
  `antlr4-python3-runtime`, and the grammars have to be kept in sync with Batfish.
- **Speed-up:** slice each config into top-level stanzas first (`interface …`, `router bgp`, `vlan`,
  `vrf`, `hostname`). Parse only those snippets.

### Option C: run the Batfish JVM without Docker
- `allinone` JAR plus Java 17, or Batfish as a library. Gives exact output, but the policy may
  forbid Java in the same way it forbids the image. Worth asking, because it's the zero-effort option.

### Recommendation
Keep **Option A** as the runtime: it's already verified against Batfish's reference output.
Use **Option B selectively**:
1. Generate the Python parsers once and use them as a **test oracle**. Run both parsers over your
   real configs and diff the vendor models, so syntax gaps in A show up automatically.
2. If a vendor shows many gaps, switch that vendor's front-end to the generated parser
   behind the same model. Junos `Hierarchy.java` (apply-groups with wildcards, `apply-path`,
   `insert`) is the first candidate, because a hand-written version is weakest there.
3. Port stages 5 and 6 verbatim whenever Batfish changes them. They're small, and every
   `Inactive_Reason` and `Configured_Status` difference traces back to them.

[Back to top](#batfish-source-analysis-what-produces-the-bgp--l3--l2-data-and-how-to-extract-it)

## 8. Mapping to bflite modules

| Batfish stage | bflite |
|---|---|
| 1 Format detection | `loader.detect_vendor` |
| 2–3 IOS/NX-OS | `parsers/tree.py`, `parsers/cisco.py` (`_nxos_preprocess` = CiscoNxosPreprocessor) |
| 2–3 Junos | `parsers/junos.py` (`flatten` = JuniperFlattener, `apply_groups` = Hierarchy, simplified) |
| 4 Conversion | `parsers/cisco.py` `_finish_*_interface`, `_ios_bgp`, `_nxos_bgp`; `parsers/junos.py` `_interfaces`, `_bgp_instance` |
| 5 Post-processing | `finalize.py` |
| 6 Topology and answerers | `questions.py` (`_Topo` = BgpTopologyUtils, `_l3_edges` = synthesizeL3Edges), `session.py`, `datamodel.py` |

[Back to top](#batfish-source-analysis-what-produces-the-bgp--l3--l2-data-and-how-to-extract-it)
