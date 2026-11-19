"""Parse every file in the given folders with a forced vendor; report crashes (robustness check)."""
import sys
import traceback
from pathlib import Path

sys.path.insert(0, __file__.rsplit("/tools/", 1)[0])
from bflite.finalize import finalize_node  # noqa: E402
from bflite.loader import parse_config  # noqa: E402

vendor = sys.argv[1]
fails = ok = 0
for d in sys.argv[2:]:
    for f in sorted(Path(d).iterdir()):
        if not f.is_file():
            continue
        try:
            n = parse_config(f.read_text(errors="replace"), f.name, vendor)
            finalize_node(n)
            ok += 1
        except Exception:
            fails += 1
            print(f"FAIL {f.name}: {traceback.format_exc().strip().splitlines()[-1]}")
print(f"{vendor}: {ok} ok, {fails} failed")
