"""Explain Batfish-vs-port differences on the stress snapshot using its manifest.

    python tools/stress_report.py out/stress/batfish \
        out/stress/bflite [--manifest testbed/stress/manifest.json] [--report FILE]

For every question and every differing column it groups the differing rows by vendor family and OS
version, and lists the edge cases (manifest quirks) that are over-represented among the nodes involved,
with a few examples. Rows only one side has are grouped the same way. That turns a diff into a work list:
"Junos 12.3 port-mode trunks", "IOS files with odd_order", and so on.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collector"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from compare import key_of, latest, load  # noqa: E402
from spec import QUESTIONS, ROW_KEYS  # noqa: E402


def node_of(row: dict) -> str:
    if "Node" in row:
        return str(row["Node"]).lower()
    m = re.match(r"(.+?)\[", str(row.get("Interface", "")))
    return m.group(1).lower() if m else "?"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("batfish")
    ap.add_argument("port")
    ap.add_argument("--manifest", default=str(Path(__file__).resolve().parent.parent / "testbed/stress/manifest.json"))
    ap.add_argument("--report")
    ap.add_argument("--show", type=int, default=3)
    a = ap.parse_args()
    man = {m["hostname"].lower(): m for m in json.loads(Path(a.manifest).read_text()) if m.get("hostname")}
    base_quirk = Counter(q for m in man.values() for q in m["quirks"])
    out = ["# Stress test: Batfish vs port, by cause", ""]

    def who(nodes: set[str]) -> str:
        grp = Counter(f"{man[n]['family']} {man[n]['model']} {man[n]['version']}" for n in nodes if n in man)
        q = Counter(x for n in nodes if n in man for x in man[n]["quirks"])
        lift = sorted(((c / len(nodes)) / (base_quirk[x] / len(man)), x, c) for x, c in q.items() if base_quirk[x])
        top = [f"{x} ({c}/{len(nodes)} nodes, x{l:.1f})" for l, x, c in reversed(lift) if l >= 2][:4]
        return (f"{len(nodes)} nodes: " + ", ".join(f"{k} {v}" for k, v in grp.most_common(5))
                + (f"\n        over-represented edge cases: {'; '.join(top)}" if top else ""))

    for q, folder, *_ in QUESTIONS:
        pr, po = latest(Path(a.batfish), folder), latest(Path(a.port), folder)
        if pr is None or po is None:
            continue
        keys = ROW_KEYS.get(q)
        ref, ours = defaultdict(list), defaultdict(list)
        for r in load(pr):
            ref[key_of(r, keys)].append(r)
        for r in load(po):
            ours[key_of(r, keys)].append(r)
        out.append(f"## {q}")
        only_b = [ref[k][0] for k in ref if k not in ours]
        only_p = [ours[k][0] for k in ours if k not in ref]
        for label, rows in (("rows only in Batfish", only_b), ("rows only in port", only_p)):
            if rows:
                out.append(f"- {label}: {len(rows)} on {who({node_of(r) for r in rows})}")
                for r in rows[: a.show]:
                    out.append(f"      e.g. {key_of(r, keys)}")
        diffs: dict[str, list] = defaultdict(list)
        for k in ref.keys() & ours.keys():
            for ra, rb in zip(sorted(ref[k], key=str), sorted(ours[k], key=str)):
                for c, v in ra.items():
                    if c in rb and rb[c] != v:
                        diffs[c].append((node_of(ra), k, v, rb[c]))
        for c, lst in sorted(diffs.items(), key=lambda kv: -len(kv[1])):
            pairs = Counter((v, w) for _, _, v, w in lst)
            out.append(f"- column {c}: {len(lst)} rows on {who({n for n, *_ in lst})}")
            for (v, w), n in pairs.most_common(a.show):
                ex = next(k for _, k, x, y in lst if (x, y) == (v, w))
                out.append(f"      {n}x batfish={v!r:.60} port={w!r:.60}  e.g. {ex}")
        if len(out) and out[-1] == f"## {q}":
            out.append("- identical")
        out.append("")
    text = "\n".join(out)
    print(text)
    if a.report:
        Path(a.report).write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
