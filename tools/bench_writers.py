"""Benchmark the last step of the pipeline: answer rows -> table -> cast to str -> JSON lines.

Three writers over the same bflite answer rows (the six questions bflite implements):
  stdlib  collector/spec.write_jsonl (what bflite_collect.py uses; no pandas)
  pandas  pybatfish-style object frame, then df.astype(str).to_json(orient="records", lines=True)
          (exactly what the production scripts do)
  polars  each cell to text in Python (pd_str: str(), lists like pandas prints them), pl.DataFrame of
          String columns, write_ndjson, then escape '/' as pandas does so the bytes can match

Each (writer, scale) runs in its own subprocess so import time and peak RSS are clean.

    python tools/bench_writers.py                 # all writers, all scales
    python tools/bench_writers.py --one stdlib 10 # one measurement (used by the driver)

Rows are scaled by repeating each answer's rows (x1, x10, x100); results go to out/bench/results.json.
"""
from __future__ import annotations

import json
import resource
import statistics
import subprocess
import sys
import time
from pathlib import Path

BF = Path(__file__).resolve().parent.parent
SNAP = BF / "testbed" / "snapshot"
OUT = BF / "out" / "bench"
QS = ["interfaceProperties", "ipOwners", "switchedVlanProperties", "bgpPeerConfiguration",
      "bgpSessionCompatibility", "bgpSessionStatus"]
SCALES = [1, 10, 100]
REPEAT = 5


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6   # macOS reports bytes


def one(writer: str, scale: int) -> dict:
    t = time.perf_counter()
    if writer == "pandas":
        import pandas as pd  # noqa: F401
    elif writer == "polars":
        import polars as pl
    import_s = time.perf_counter() - t

    sys.path[:0] = [str(BF), str(BF / "collector")]
    from bflite import Session
    from bflite.datamodel import pd_str
    from bflite.questions import Table
    from spec import IP_OWNERS_COLUMNS, write_jsonl

    bf = Session()
    bf.init_snapshot(str(SNAP), name="live")
    tables = []
    for q in QS:
        a = getattr(bf.q, q)().answer()
        rows, cols = a.rows, a.columns
        if q == "ipOwners":
            cols, rows = IP_OWNERS_COLUMNS, sorted(rows, key=lambda r: str(r["IP"]))
        tables.append((q, cols, rows * scale))
    nrows = sum(len(r) for _, _, r in tables)
    dest = OUT / f"{writer}_x{scale}"
    dest.mkdir(parents=True, exist_ok=True)
    rss_before = rss_mb()

    def run():
        for q, cols, rows in tables:
            path = dest / f"{q}.json"
            if writer == "stdlib":
                write_jsonl(path, cols, rows)
            elif writer == "pandas":
                df = Table(rows, cols).to_frame()
                df.astype(str).to_json(path, orient="records", lines=True)
            else:
                df = pl.DataFrame({c: [pd_str(r.get(c)) for r in rows] for c in cols},
                                  schema={c: pl.String for c in cols})
                path.write_text(df.write_ndjson().replace("/", "\\/"))

    times = []
    for _ in range(REPEAT):
        t = time.perf_counter()
        run()
        times.append(time.perf_counter() - t)
    return {"writer": writer, "scale": scale, "rows": nrows, "import_s": round(import_s, 3),
            "median_s": round(statistics.median(times), 4), "min_s": round(min(times), 4),
            "rss_before_mb": round(rss_before, 1), "rss_peak_mb": round(rss_mb(), 1)}


def same_records(a: Path, b: Path) -> tuple[bool, bool]:
    """(byte-identical, identical after treating null and "None" as equal)."""
    ba, bb = a.read_bytes(), b.read_bytes()
    if ba == bb:
        return True, True
    norm = lambda p: [{k: ("None" if v is None else v) for k, v in json.loads(l).items()}
                      for l in p.read_text().splitlines() if l.strip()]
    return False, norm(a) == norm(b)


def driver() -> None:
    results = []
    for scale in SCALES:
        for w in ("stdlib", "pandas", "polars"):
            p = subprocess.run([sys.executable, __file__, "--one", w, str(scale)], capture_output=True, text=True)
            if p.returncode:
                print(p.stderr, file=sys.stderr)
                raise SystemExit(1)
            r = json.loads(p.stdout.strip().splitlines()[-1])
            results.append(r)
            print(json.dumps(r), flush=True)
    print("\nOutput check against the pandas (production) bytes, scale x1:")
    for w in ("stdlib", "polars"):
        for q in QS:
            byte, sem = same_records(OUT / "pandas_x1" / f"{q}.json", OUT / f"{w}_x1" / f"{q}.json")
            print(f"  {w:7s} {q:24s} bytes={'same' if byte else 'differ'} records={'same' if sem else 'DIFFER'}")
    (OUT / "results.json").write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--one":
        print(json.dumps(one(sys.argv[2], int(sys.argv[3]))))
    else:
        driver()
