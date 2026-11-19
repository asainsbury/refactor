# Handoff: finish the Batfish to Python port on the laptop

Written 2026-11-19 on the home laptop (with Docker for the Batfish reference). Kept as history: at work, start
with AGENTS.md, which supersedes the steps below.

<!-- toc -->
**Contents**

- [What's in this folder](#whats-in-this-folder)
- [Steps](#steps)
- [Progress](#progress)
- [Work left (in order)](#work-left-in-order)
- [Prompt for Claude Code in VS Code](#prompt-for-claude-code-in-vs-code)
<!-- /toc -->

## What's in this folder

| Path | What it is | State |
|---|---|---|
| `batfish.md` | Topic note: what we collect, why, the plan | Written |
| `docs/BATFISH_SOURCE_MAP.md` | Which Batfish Java/ANTLR code decides each output value, extraction options | Written |
| `docs/BFLITE_README.md` | How the Python port works, what was verified, known gaps | Written |
| `bflite/` | The Python port (pure Python, pandas only needed for `.frame()`) | 6 of 10 questions done |
| `tests/` | pytest: Batfish's own example-network answers plus a small multi-vendor sample | 10 passing |
| `testbed/generate.py` | Generates the 50-device test bed (26 Cisco, 24 Juniper) with nine deliberate faults | Written, checked |
| `collector/spec.py` | The ten questions, folders, file names and quirks from the pipeline README; JSONL writer | Written |
| `collector/pybatfish_collect.py` | Reference: real Batfish via pybatfish, written exactly like production | Written, not run |
| `collector/bflite_collect.py` | Same files from the Python port, no Batfish, no pandas | Written, checked |
| `tools/compare.py` | Diffs the two output folders per question and column | Written |
| `run_batfish.sh` | Starts Docker, generates configs, runs Batfish, collects | Written, not run |

`out/` and `testbed/snapshot/` are generated; add them to `.gitignore` if you don't want them committed.
The note still needs its Contents block: run `python scripts/topic_toc.py batfish.md topics/_index.md`.

[Back to top](#handoff-finish-the-batfish-to-python-port-on-the-laptop)

## Steps

1. **Docker Desktop:** give it at least 6 GB RAM (Settings, Resources).
2. **Reference answers:** `zsh run_batfish.sh`. The first pull is about 1.5 GB.
3. **Port answers:** `python collector/bflite_collect.py --snapshot testbed/snapshot --out out/bflite --profile-memory`
4. **Compare:** `python tools/compare.py out/batfish out/bflite --report out/compare.md`
5. **Tests:** `python -m pytest tests`. This needs pandas and pytest; pytest is pinned already, pandas isn't. Ask before installing, per AGENTS.md.

[Back to top](#handoff-finish-the-batfish-to-python-port-on-the-laptop)

## Progress

**2026-11-19, Intel Mac (Python 3.13):** steps 1 to 5 run. Reference answers are in `out/batfish/`, and the
`bflite-batfish` container was left running for probes (`out/probe/probe.py` shows the pattern). pandas 3.1.1
and polars 2.1.0 are pinned in `requirements.txt`; `tests/conftest.py` puts `bflite` on the path. Item 1 is
done: all six ported questions match Batfish 100% (`out/compare.md`), 21 tests pass, and the rules found are
in `batfish.md`, "What the first comparison found". The Batfish source can be cloned sparsely
(see docs/BATFISH_SOURCE_MAP.md). `tools/bench_writers.py` compares the stdlib, pandas and polars
writers.

[Back to top](#handoff-finish-the-batfish-to-python-port-on-the-laptop)

## Work left (in order)

1. ~~**Fix differences on the six ported questions** until compare shows 100%.~~ Done 2026-11-19.
1a. ~~**Stress-test edge cases**~~ Done 2026-11-19: every port-side case in `batfish.md`, "Stress test:
   1,000 configs", is fixed and has a rule test (32 tests pass). Five questions match Batfish on all 1,000
   configs; bgpSessionStatus is at 90.6% until routes exist. To rerun: `testbed/generate_stress.py`, the
   reference collector into `out/stress/batfish` (about 4 minutes, about 6 GiB in Docker), `bflite_collect.py`
   into `out/stress/bflite`, then `tools/stress_report.py`.
2. **routes:** add a small dataplane to `bflite`: connected, local, static (including Null0/discard), then BGP
   (eBGP/iBGP, network statements, Junos export policies, NX-OS/IOS route-maps with prefix-lists, multipath,
   AS-path loop check, next-hop-self). Iterate to a fixed point, as Batfish does. Columns (current Batfish,
   main RIB): `Node, VRF, Network, Next_Hop, Protocol, Metric, Admin_Distance, Tag`. `Next_Hop` strings:
   `ip X`, `interface NAME`, `interface NAME ip X`, `discard`. Take admin distances and protocol names from the
   reference output.
3. **bgpSessionStatus:** once there's a RIB, replace the L3-graph approximation with Batfish's rule
   (a session is established when each side has a route to the other's address).
4. **nodeProperties:** one row per node, 37 columns (`NodePropertySpecifier.java`). Batfish-generated names
   (for example routing policies it creates per BGP peer) are the hard part; copy the naming from the
   reference output.
5. **initIssues / parseWarning:** these report Batfish's ANTLR parser problems. A different parser can't
   reproduce them line for line. Emit our own unrecognised-line warnings with the same columns, and expect an
   empty or near-empty reference on this test bed.
6. **Speed and memory:** keep everything O(lines) to parse and roughly O(peers + prefixes × neighbours) for the
   dataplane. Index peers by (node, VRF) and owners by IP instead of nested scans. Write JSONL straight from
   rows (no pandas). Record time and peak memory in `out/*/timings.json` against Batfish's numbers.

[Back to top](#handoff-finish-the-batfish-to-python-port-on-the-laptop)

## Prompt for Claude Code in VS Code

Paste this into Claude Code with `~/projects/topics` open:

> Read `HANDOFF.md`, `batfish.md` and `docs/BATFISH_SOURCE_MAP.md`.
> Follow AGENTS.md.
> 1) Run `run_batfish.sh` to get real Batfish answers for the 50-device test bed.
> 2) Run the bflite collector and `tools/compare.py`, and show me the summary table.
> 3) Work through "Work left" in HANDOFF.md one item at a time. After each item, rerun the collector, compare
>    and pytest, and update the status table in batfish.md. Stop and ask before any change that alters
>    production file names or column order.
> 4) Finish with timings and peak memory for Batfish vs bflite in batfish.md.
> Don't commit or push until I ask.

[Back to top](#handoff-finish-the-batfish-to-python-port-on-the-laptop)
