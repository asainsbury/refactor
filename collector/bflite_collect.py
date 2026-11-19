"""Python-port collector: same questions, same files, no Batfish, no pandas.

    python collector/bflite_collect.py \
        --snapshot testbed/snapshot --out out/bflite
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import tracemalloc
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
from spec import IP_OWNERS_COLUMNS, QUESTIONS, out_path, utc_stamp, write_jsonl  # noqa: E402

from bflite import Session  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--profile-memory", action="store_true", help="report peak Python memory (slower)")
    a = ap.parse_args()

    if a.profile_memory:
        tracemalloc.start()
    timings: dict = {}
    t0 = time.perf_counter()
    bf = Session()
    bf.init_snapshot(a.snapshot, name="live", overwrite=True)
    timings["init_snapshot"] = round(time.perf_counter() - t0, 3)

    stamp = utc_stamp()
    root = Path(a.out)
    missing = []
    for q, *_ in QUESTIONS:
        t = time.perf_counter()
        try:
            ans = getattr(bf.q, q)().answer()
        except NotImplementedError:
            missing.append(q)
            print(f"{q:26s}  not implemented in bflite yet")
            continue
        rows, cols = ans.rows, ans.columns
        if q == "ipOwners":
            cols = IP_OWNERS_COLUMNS
            rows = sorted(rows, key=lambda r: str(r["IP"]))
        n = write_jsonl(out_path(root, q, stamp), cols, rows)
        timings[q] = round(time.perf_counter() - t, 3)
        print(f"{q:26s} {n:6d} rows  {timings[q]:7.3f}s")
    timings["total"] = round(time.perf_counter() - t0, 3)
    if a.profile_memory:
        timings["peak_mem_mb"] = round(tracemalloc.get_traced_memory()[1] / 1e6, 1)
    timings["not_implemented"] = missing
    root.mkdir(parents=True, exist_ok=True)
    (root / "timings.json").write_text(json.dumps(timings, indent=1))
    print(f"total {timings['total']}s" + (f", peak {timings['peak_mem_mb']} MB" if a.profile_memory else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
