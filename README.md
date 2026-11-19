# bflite

Pure-Python stand-in for the Batfish questions our config pipeline collects: same questions, same JSONL files,
no Batfish server. Matches real Batfish 100% on interface, IP owner, VLAN and BGP configuration questions over a
1,000-config multi-vendor test bed (IOS/IOS-XE, NX-OS, Junos).

Agents and developers: read `AGENTS.md` first. Design and findings: `batfish.md`.
