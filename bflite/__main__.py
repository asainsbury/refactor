"""CLI:  python -m bflite <snapshot_dir> <question> [--nodes REGEX] [--out file.csv|file.json]"""
import argparse
import sys

from .session import Session

QUESTIONS = ["bgpProcessConfiguration", "bgpPeerConfiguration", "bgpSessionCompatibility", "bgpSessionStatus",
             "interfaceProperties", "ipOwners", "layer3Edges", "switchedVlanProperties"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bflite")
    ap.add_argument("snapshot")
    ap.add_argument("question", choices=QUESTIONS)
    ap.add_argument("--nodes")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    bf = Session()
    bf.init_snapshot(a.snapshot)
    q = getattr(bf.q, a.question)
    df = (q(nodes=a.nodes) if a.nodes and a.question != "ipOwners" else q()).answer().frame()
    import pandas as pd

    def conv(v):
        if isinstance(v, list):
            return [str(x) for x in v]
        return v if isinstance(v, (str, int, float, bool, type(None))) else str(v)
    df = pd.DataFrame({c: pd.Series([conv(v) for v in df[c]], dtype=object) for c in df.columns})
    if a.out and a.out.endswith(".json"):
        df.to_json(a.out, orient="records", indent=1)
    elif a.out:
        df.to_csv(a.out, index=False)
    else:
        print(df.to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
