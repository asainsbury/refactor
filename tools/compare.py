"""Compare two collector output folders (Batfish reference vs Python port), question by question.

    python tools/compare.py out/batfish out/bflite \
        [--question routes] [--show 20] [--report out/compare.md]

Row order and column order are ignored. Rows are matched on the key columns in collector/spec.py, then
every other column is compared. Output: per question, rows only on one side, and per-column mismatch counts
with examples, which is the work list for the port.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collector"))
from spec import QUESTIONS, ROW_KEYS  # noqa: E402


def latest(root: Path, folder: str) -> Path | None:
    files = sorted((root / folder).glob("*.json"))
    return files[-1] if files else None


def load(path: Path) -> list[dict]:
    """pandas >= 3 writes a missing value as null after astype(str); older pandas writes "None".
    Normalise to "None" so either side's pandas version doesn't matter."""
    with open(path, encoding="utf-8") as f:
        return [{k: ("None" if v is None else v) for k, v in json.loads(line).items()} for line in f if line.strip()]


def key_of(r: dict, keys: list[str] | None) -> tuple:
    if keys is None:
        return tuple(sorted(r.items()))
    return tuple(r.get(k) for k in keys)


def compare(ref_rows, our_rows, keys, show):
    """Rows with the same key are compared as a multiset: identical rows pair off first, the rest pair in
    sorted order and count per-column differences, and any surplus counts as rows only on one side."""
    out = []
    cols_ref = list(ref_rows[0]) if ref_rows else []
    cols_our = list(our_rows[0]) if our_rows else []
    if set(cols_ref) != set(cols_our) and ref_rows and our_rows:
        out.append(f"  columns only in Batfish: {sorted(set(cols_ref) - set(cols_our))}")
        out.append(f"  columns only in port:    {sorted(set(cols_our) - set(cols_ref))}")
    elif cols_ref != cols_our and ref_rows and our_rows:
        out.append("  column order differs (values are compared regardless)")
    ref = defaultdict(list)
    ours = defaultdict(list)
    for r in ref_rows:
        ref[key_of(r, keys)].append(r)
    for r in our_rows:
        ours[key_of(r, keys)].append(r)
    only_ref = [k for k in ref if k not in ours]
    only_our = [k for k in ours if k not in ref]
    col_diff: Counter = Counter()
    examples: dict[str, list] = defaultdict(list)
    matched = surplus_ref = surplus_our = 0
    as_tuple = lambda r: tuple(r.get(c) for c in cols_ref)  # noqa: E731
    for k in ref.keys() & ours.keys():
        sa, sb = Counter(map(as_tuple, ref[k])), Counter(map(as_tuple, ours[k]))
        same = sa & sb
        matched += sum(same.values())
        ra, rb = sorted((sa - sb).elements(), key=str), sorted((sb - sa).elements(), key=str)
        for a, b in zip(ra, rb):
            for c, x, y in zip(cols_ref, a, b):
                if x != y:
                    col_diff[c] += 1
                    if len(examples[c]) < show:
                        examples[c].append((k, x, y))
        surplus_ref += max(0, len(ra) - len(rb))
        surplus_our += max(0, len(rb) - len(ra))
    out.insert(0, f"  rows: batfish={len(ref_rows)} port={len(our_rows)} | keys matched={len(ref.keys() & ours.keys())}"
                  f" identical rows={matched} | keys only batfish={len(only_ref)} only port={len(only_our)}"
                  + (f" | extra rows under shared keys: batfish={surplus_ref} port={surplus_our}"
                     if surplus_ref or surplus_our else ""))
    for k in only_ref[:show]:
        out.append(f"    - only in Batfish: {k}")
    for k in only_our[:show]:
        out.append(f"    + only in port:    {k}")
    for c, n in col_diff.most_common():
        out.append(f"  column {c}: {n} differ")
        for k, a, b in examples[c]:
            out.append(f"      {k}: batfish={a!r} port={b!r}")
    score = matched / len(ref_rows) if ref_rows else (1.0 if not our_rows else 0.0)
    return score, out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("batfish")
    ap.add_argument("port")
    ap.add_argument("--question")
    ap.add_argument("--show", type=int, default=5)
    ap.add_argument("--report")
    a = ap.parse_args()
    lines = ["# Batfish vs Python port", "", "| Question | Identical rows | Notes |", "|---|---|---|"]
    detail = []
    worst = 1.0
    for q, folder, *_ in QUESTIONS:
        if a.question and q != a.question:
            continue
        pr, po = latest(Path(a.batfish), folder), latest(Path(a.port), folder)
        if pr is None or po is None:
            lines.append(f"| {q} | – | missing output ({'batfish' if pr is None else 'port'}) |")
            worst = 0.0
            continue
        score, out = compare(load(pr), load(po), ROW_KEYS.get(q), a.show)
        worst = min(worst, score)
        lines.append(f"| {q} | {score:.1%} | {pr.name} vs {po.name} |")
        detail += [f"## {q}", "```", *out, "```", ""]
    text = "\n".join(lines + [""] + detail)
    print(text)
    if a.report:
        Path(a.report).write_text(text)
    return 0 if worst == 1.0 else 1


if __name__ == "__main__":
    sys.exit(main())
