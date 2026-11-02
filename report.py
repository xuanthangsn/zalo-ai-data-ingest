"""Phase 1 coverage report, read from the cached profiles.

    python -m datamate.ingest.report              # one line per business
    python -m datamate.ingest.report --failures   # the normaliser's to-do list
    python -m datamate.ingest.report --business biz_117 --schema
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from collections import Counter


def load_profiles(build: str) -> list[dict]:
    out = []
    for pf in sorted(glob.glob(os.path.join(build, "*", "profile.json"))):
        try:
            with open(pf, encoding="utf-8") as fh:
                out.append(json.load(fh))
        except json.JSONDecodeError:
            print(f"!! unreadable (still being written?): {pf}")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default="build")
    ap.add_argument("--failures", action="store_true")
    ap.add_argument("--business", default=None)
    ap.add_argument("--schema", action="store_true",
                    help="print the schema as the prompt would show it")
    a = ap.parse_args(argv)

    profs = load_profiles(a.build)
    if a.business:
        profs = [p for p in profs if p["business_id"] == a.business]

    if a.schema:
        from ..run import Business
        for p in profs:
            biz = Business(a.build, p["business_id"])
            print(f"### {p['business_id']} - {p.get('description', '')}")
            print(f"### data to {p.get('data_end')}, "
                  f"range {biz.range[0]}..{biz.range[1]}")
            print(biz.schema_text())
            print()
            biz.close()
        return

    fails: Counter = Counter()
    low: list = []
    kinds: Counter = Counter()
    n_rows = 0

    print(f"{'business':11s} {'tbl':>3s} {'rows':>9s} {'ids_filled':>10s} "
          f"{'carried':>8s} {'dup_drop':>8s}  data_end")
    for p in profs:
        rows = sum(t["rows"] for t in p["tables"])
        filled = sum(t.get("order_ids_filled") or 0 for t in p["tables"])
        carried = sum(t["cells_carried_forward"] for t in p["tables"])
        dups = sum(t["duplicate_rows_dropped"] for t in p["tables"])
        n_rows += rows
        for t in p["tables"]:
            for c in t["columns"]:
                kinds[c["kind"]] += 1
                pr = c.get("parse_rate")
                if pr is not None and pr < 0.999:
                    low.append((p["business_id"], t["name"], c["sql"], c["kind"],
                                round(pr, 4), list((c.get("parse_failures") or {}))[:3]))
                for v, k in (c.get("parse_failures") or {}).items():
                    fails[(c["kind"], v)] += k
        print(f"{p['business_id']:11s} {len(p['tables']):3d} {rows:9d} {filled:10d} "
              f"{carried:8d} {dups:8d}  {p.get('data_end')}")

    print(f"\nbusinesses: {len(profs)}   rows ingested: {n_rows:,}")
    print("column kinds:", dict(kinds.most_common()))
    print(f"\ncolumns with parse rate < 99.9%: {len(low)}")
    for row in sorted(low, key=lambda r: r[4])[:25]:
        print("   ", row)
    if a.failures:
        print(f"\nunparsed values ({len(fails)} distinct, "
              f"{sum(fails.values()):,} cells):")
        for (kind, v), n in fails.most_common(60):
            print(f"    {kind:9s} {n:8d}  {v!r}")


if __name__ == "__main__":
    main()
