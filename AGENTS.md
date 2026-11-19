# Agent instructions: bflite (Batfish questions in pure Python)

Start here. `HANDOFF.md` is the history from the home laptop; this file supersedes its steps.

## What this is

`bflite` answers the pybatfish questions our scheduled pipeline collects, from the same device configs, without
the Batfish server or its Docker image (which can't run here). The goal is the same files with the same rows and
values, so nothing downstream changes. `collector/spec.py` holds the pipeline's ten questions, folder names, file
names and quirks; `collector/bflite_collect.py` writes them.

State on 2026-11-19, measured against real Batfish (`batfish/allinone`, pybatfish 2026.08.27, pandas 3.0.5):

| Question | 50-device test bed | 1,000-config stress bed |
|---|---|---|
| interfaceProperties, ipOwners, switchedVlanProperties | 100% | 100% |
| bgpPeerConfiguration, bgpSessionCompatibility | 100% | 100% |
| bgpSessionStatus | 100% | 90.6% (the rest need a RIB) |
| routes, nodeProperties, initIssues, parseWarning | not built | not built |

Every Batfish rule the port copies is listed in `batfish.md` with its source, and pinned by a test in
`tests/test_batfish_rules.py`.

## Rules

- **Real configs and real output stay out of this repo.** Keep them in a folder outside it, or under `out/`
  (gitignored). Tests use small invented configs with documentation addresses, never real hostnames or IPs.
- **Ask before installing packages.** Runtime needs only the standard library; pandas is used by `.frame()` and
  the tests; polars only by `tools/bench_writers.py`.
- **Batfish is the reference, not the vendor docs.** When the port and Batfish differ, copy Batfish, even where
  Batfish is wrong about the device. Find the rule in the Batfish source (`docs/BATFISH_SOURCE_MAP.md` says where;
  github.com/batfish/batfish, Apache-2.0), fix the port, and add a test.
- **Don't change output file names, folder names or column order** without asking: downstream depends on them.

## Setup

```sh
python3 -m venv .venv && . .venv/bin/activate      # Python 3.9 or later
pip install -r requirements.txt
python -m pytest tests -q                         # expect 32 passed
python testbed/generate.py                        # 50-device test bed -> testbed/snapshot/
python collector/bflite_collect.py --snapshot testbed/snapshot --out out/bflite
```

## First job: compare against production

Here the pipeline's own Batfish output is the reference, so no Docker is needed:

1. Take one pipeline run: the configs it used (the snapshot folder) and the JSONL it wrote for each question.
   Put them outside the repo or under `out/prod/`, keeping the pipeline's folder layout.
2. `python collector/bflite_collect.py --snapshot <that snapshot> --out out/port`
3. `python tools/compare.py out/prod out/port --report out/compare.md`
4. For each differing column, find the Batfish rule, fix it, add a test with an invented config, rerun.
   `tools/stress_report.py` shows how to group differences by OS version and feature; real configs have no
   manifest, so group by node model and version from `show version` or the inventory instead.
5. Check which pandas the pipeline runs: pandas 3 writes missing values as `null`, older pandas as `"None"`.
   bflite writes `"None"`. `compare.py` treats them as equal, but a downstream reader may not.

## Work left, in order

1. Production comparison above, until the six ported questions match.
2. **routes:** connected, local and static first, then BGP (eBGP/iBGP, network statements, Junos export policies,
   NX-OS/IOS route-maps with prefix-lists, multipath, AS-path loop check, next-hop-self), iterated to a fixed
   point as Batfish does. Columns: `Node, VRF, Network, Next_Hop, Protocol, Metric, Admin_Distance, Tag`.
3. **bgpSessionStatus:** replace the L3-adjacency check with Batfish's rule (each side has a route to the
   other); this clears the remaining 182 stress-bed rows.
4. **nodeProperties:** 37 columns; copy Batfish's generated policy names from real output.
5. **initIssues, parseWarning:** emit our own unrecognised-line warnings with Batfish's columns.
6. Output and speed (from profiling 1,000 configs): a `--missing none|null` switch; a byte-level test of the
   writer against pandas; one bad config becomes an `initIssues` row instead of stopping the run; atomic
   writes (temp file, then rename); a fixed row order; build interfaceProperties rows without re-sorting each
   row; precompute BGP reachability once; cache `Line.words` and IP objects.

## Files

| Path | What it is |
|---|---|
| `bflite/` | The port: parsers (`parsers/cisco.py`, `parsers/junos.py`), conversion rules, `finalize.py` (post-processing), `questions.py` (answers) |
| `collector/` | `spec.py` (the pipeline contract), `bflite_collect.py` (port), `pybatfish_collect.py` (reference, needs a Batfish server) |
| `tools/compare.py` | Diff two output folders per question and column |
| `tools/stress_port.py`, `tools/stress_report.py` | Crash/speed check and grouped diff for the generated stress bed |
| `tools/bench_writers.py` | stdlib vs pandas vs polars writer benchmark |
| `testbed/generate.py`, `testbed/generate_stress.py` | 50-device test bed; 1,000-config stress bed with a manifest of edge cases |
| `tests/` | Regression tests; `batfish_example/` is Batfish's own example network with its reference answers |
| `batfish.md` | Design, status, every Batfish rule found, timings |
| `docs/` | Batfish source map; bflite internals |
| `run_batfish.sh` | Home only: real Batfish in Docker for reference answers |
