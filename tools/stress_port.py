"""Robustness and speed check of bflite on a generated stress snapshot (testbed/generate_stress.py).

    python tools/stress_port.py [testbed/stress]

Parses every file on its own, the way loader.load_snapshot does, and reports:
  * crashes, grouped by exception, with the file and the line of bflite code that raised;
  * vendor detection that disagrees with the manifest (junk files are expected to be skipped);
  * hostnames that differ from the manifest (Batfish lower-cases hostnames);
  * the slowest files, total parse time and peak memory.
Then it runs every implemented question on the whole snapshot, so question code meets the same edge cases.
"""
from __future__ import annotations

import json
import resource
import sys
import time
import traceback
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from bflite import Session  # noqa: E402
from bflite.finalize import finalize_node  # noqa: E402
from bflite.loader import detect_vendor, parse_config, read_config  # noqa: E402

FAMILY = {"CISCO_IOS": "ios", "CISCO_NXOS": "nxos", "JUNIPER": "junos", None: None}


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "testbed" / "stress"
    manifest = {m["file"]: m for m in json.loads((root / "manifest.json").read_text())}
    cfg = root / "snapshot" / "configs"
    crashes: dict[str, list] = defaultdict(list)
    misdetect, hostdiff, times = [], [], []
    t_all = time.perf_counter()
    for f in sorted(cfg.iterdir()):
        m = manifest.get(f.name, {})
        text = read_config(f)
        t = time.perf_counter()
        try:
            vendor = detect_vendor(text)
            fam = FAMILY.get(vendor, vendor)
            if fam != m.get("family"):
                misdetect.append((f.name, m.get("family"), fam))
            node = parse_config(text, f.name, vendor)
            if node is not None:
                finalize_node(node)
                want = (m.get("hostname") or "").lower()
                if want and node.hostname != want and "no_hostname" not in m.get("quirks", []):
                    hostdiff.append((f.name, want, node.hostname))
        except Exception as e:  # noqa: BLE001
            tb = traceback.extract_tb(e.__traceback__)
            where = next((f"{Path(x.filename).name}:{x.lineno} {x.name}" for x in reversed(tb) if "bflite" in x.filename),
                         "?")
            crashes[f"{type(e).__name__}: {e} @ {where}"].append((f.name, m.get("model"), m.get("version")))
        times.append((time.perf_counter() - t, f.name, m.get("lines", 0)))
    parse_s = time.perf_counter() - t_all
    print(f"files: {len(times)} | parse+finalize {parse_s:.2f}s | crashes: {sum(len(v) for v in crashes.values())}")
    for k, v in sorted(crashes.items(), key=lambda kv: -len(kv[1])):
        print(f"  CRASH x{len(v)}: {k}")
        for x in v[:4]:
            print(f"      {x}")
    print(f"vendor detection differs from manifest: {len(misdetect)}")
    for x in misdetect[:20]:
        print(f"  {x[0]}: manifest={x[1]} detected={x[2]}")
    print(f"hostname differs from manifest: {len(hostdiff)}")
    for x in hostdiff[:10]:
        print(f"  {x}")
    print("slowest files:")
    for s, n, lines in sorted(times, reverse=True)[:5]:
        print(f"  {s * 1000:7.1f} ms  {n} ({lines} lines)")

    t = time.perf_counter()
    bf = Session()
    try:
        bf.init_snapshot(str(root / "snapshot"), name="stress")
    except Exception:  # noqa: BLE001
        print("init_snapshot CRASHED:\n" + traceback.format_exc())
        return 1
    print(f"init_snapshot {time.perf_counter() - t:.2f}s")
    for q in ("interfaceProperties", "ipOwners", "switchedVlanProperties", "bgpPeerConfiguration",
              "bgpSessionCompatibility", "bgpSessionStatus"):
        t = time.perf_counter()
        try:
            n = len(getattr(bf.q, q)().answer().rows)
            print(f"  {q:24s} {n:7d} rows {time.perf_counter() - t:7.2f}s")
        except Exception:  # noqa: BLE001
            print(f"  {q:24s} CRASHED:\n" + traceback.format_exc())
    print(f"peak RSS {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6:.0f} MB")
    return 1 if crashes else 0


if __name__ == "__main__":
    sys.exit(main())
