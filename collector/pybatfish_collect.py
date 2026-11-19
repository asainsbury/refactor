"""Reference collector: real Batfish via pybatfish, written the same way as the production pipeline.

Runs inside the batfish/allinone container (it ships pybatfish), started by run_batfish.sh:
    python /work/collector/pybatfish_collect.py --snapshot /work/testbed/snapshot --out /work/out/batfish
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from spec import IP_OWNERS_COLUMNS, QUESTIONS, out_path, utc_stamp  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--network", default="TESTBED")
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--name", default="live")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    from pybatfish.client.session import Session

    bf = None
    for attempt in range(60):                       # the service takes a while to come up
        try:
            bf = Session(host=a.host)
            bf.set_network(a.network)
            break
        except Exception as e:  # noqa: BLE001
            print(f"waiting for Batfish ({e.__class__.__name__})", flush=True)
            time.sleep(5)
    if bf is None:
        print("Batfish did not come up", file=sys.stderr)
        return 1

    timings = {}
    t0 = time.perf_counter()
    bf.init_snapshot(a.snapshot, name=a.name, overwrite=True)
    timings["init_snapshot"] = round(time.perf_counter() - t0, 2)

    stamp = utc_stamp()
    root = Path(a.out)
    for q, *_ in QUESTIONS:
        t = time.perf_counter()
        df = getattr(bf.q, q)().answer().frame()
        timings[q] = round(time.perf_counter() - t, 2)
        if q == "ipOwners":
            df = df[IP_OWNERS_COLUMNS].sort_values("IP")
        path = out_path(root, q, stamp)
        path.parent.mkdir(parents=True, exist_ok=True)
        df.astype(str).to_json(path, orient="records", lines=True)
        print(f"{q:26s} {len(df):6d} rows  {timings[q]:7.2f}s  -> {path.relative_to(root)}", flush=True)
    timings["total"] = round(time.perf_counter() - t0, 2)
    (root / "timings.json").write_text(json.dumps(timings, indent=1))
    print(f"total {timings['total']}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
