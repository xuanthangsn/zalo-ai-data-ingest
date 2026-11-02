"""The two ingestion passes, and the one writer that turns them into SQLite.

Pass 1 takes a census of every column; the classifier decides each column's
kind; pass 2 parses the cells with the pure parser for that kind. The table
shapes and the shop's own column names are kept exactly as they are.

Rulebook s8 is applied here, not downstream:
  * blank cells on later rows of an order group mean "same as the order"
  * two rows of the SAME order identical in every cell are an export bug
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import unicodedata
from collections import Counter, defaultdict

from .. import normalize as N
from . import classify as CL
from . import columns as C
from . import kinds as K
from . import readers as RD
from . import readme as RM

GENERIC_SHEETS = {"sheet1", "sheet", "sheet0", "data", "table1", "worksheet", "trang1"}

# An order id, not a price ("don gia") or a unit ("don vi tinh").
ORDER_ID_EXACT = re.compile(
    r"^(?:ma|so)\s*(?:don|don hang|hoa don|hd|bill|ct|chung tu|phieu|phieu nhap|"
    r"phieu thu|invoice)$|^(?:don so|so ct|so hd|so bill|so hoa don|so phieu|"
    r"ma ?don ?hang|ma ?hoa ?don|ma ?bill|ma ?don|ma ?phieu|hd|order ?id)$|^(?:hoa don|don hang|bill)$",
    re.I)
ORDER_ID_LOOSE = re.compile(
    r"\b(?:ma|so)\s*(?:don|hoa don|hd|bill|ct|chung tu|phieu|invoice)\b|\bdon so\b", re.I)
NOT_ID_RX = re.compile(
    r"vi tinh|\bgia\b|tien|luong|ngay|gio|khach|hang hoa|nhan vien|barcode|"
    r"trang thai|nhom|dia chi|phone|sdt|email|don vi|ghi chu|ly do|note", re.I)
ID_VALUE_RX = re.compile(r"^[A-Za-z]{2,6}[-_]?\d{3,}$")
LINE_NO_RX = re.compile(r"^(stt|no\.?|so thu tu|dong|line|sothutu|thu tu|idx)$", re.I)


def ascii_name(s: str, fallback: str = "col") -> str:
    s = unicodedata.normalize("NFD", str(s).lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.replace("đ", "d").replace("Đ", "d")
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    s = re.sub(r"_+", "_", s)
    if not s or s[0].isdigit():
        s = f"{fallback}_{s}" if s else fallback
    return s[:56]


def _uniq(name: str, used: set[str]) -> str:
    base, i = name, 2
    while name in used:
        name = f"{base}_{i}"
        i += 1
    used.add(name)
    return name


# --------------------------------------------------------------------------- #
# group raw tables that share a header into one logical table
# --------------------------------------------------------------------------- #
def _sig(t: RD.RawTable) -> tuple:
    return tuple(ascii_name(h) for h in t.header)


def group_tables(tables: list[RD.RawTable]) -> list[tuple[str, list[RD.RawTable]]]:
    groups: dict[tuple, list[RD.RawTable]] = defaultdict(list)
    for t in tables:
        groups[_sig(t)].append(t)

    out, used = [], set()
    for _sg, ts in groups.items():
        sheets = {t.sheet for t in ts if t.sheet}
        stems = {re.sub(r"\.(csv|xlsx|sqlite)$", "", t.file, flags=re.I) for t in ts}
        name = None
        if len(sheets) == 1:
            only = next(iter(sheets))
            if ascii_name(only) not in GENERIC_SHEETS:
                name = only
        if name is None and len(stems) == 1:
            name = next(iter(stems))
        if name is None:
            pref = os.path.commonprefix(sorted(stems)).rstrip("_-. 0123456789")
            name = pref or (next(iter(sheets)) if sheets else "table")
        out.append((_uniq(ascii_name(name, "t"), used), sorted(
            ts, key=lambda t: (t.year_hint or 0, t.month_hint or 0, t.file, t.sheet or ""))))
    return out


# --------------------------------------------------------------------------- #
# rulebook s8: carry-forward and duplicate rows inside an order
# --------------------------------------------------------------------------- #
def find_order_col(header: list[str], kinds: list[str], rows: list[list]) -> int | None:
    cands = []
    for i, h in enumerate(header):
        if kinds[i] != "text":
            continue
        key = N.nd(h).replace("_", " ")
        if LINE_NO_RX.match(key) or NOT_ID_RX.search(key):
            continue
        exact = bool(ORDER_ID_EXACT.match(key))
        loose = bool(ORDER_ID_LOOSE.search(key))
        if not (exact or loose):
            continue
        vals = [r[i] for r in rows[:5000] if r[i] is not None]
        if len(set(map(str, vals))) < 2:
            continue
        shaped = sum(1 for v in vals[:400] if ID_VALUE_RX.match(str(v))) / max(1, len(vals[:400]))
        cands.append((2 if exact else 0, shaped, -i, i))
    if not cands:
        return None
    cands.sort(reverse=True)
    return cands[0][3]


def apply_order_rules(header, kinds, rows, order_col, ndata=None,
                      parts=None) -> tuple[list[list], dict]:
    stats = {"carried": 0, "dup_dropped": 0, "order_level_cols": [],
             "order_ids_filled": 0}
    if order_col is None or not rows:
        return rows, stats

    # Rulebook s8, applied to the grouping key FIRST: exports blank the order
    # columns on the continuation lines of a multi-line order. Without this the
    # continuation rows all share a NULL key, which both breaks order-level
    # detection and makes the duplicate rule delete real sale lines.
    prev_part = object()
    last = None
    for idx, r in enumerate(rows):
        part = parts[idx] if parts else None
        if part != prev_part:
            prev_part, last = part, None
        v = r[order_col]
        if v is None or (isinstance(v, str) and not v.strip()):
            if last is not None:
                r[order_col] = last
                stats["order_ids_filled"] += 1
        else:
            last = v

    groups: dict[object, list[int]] = defaultdict(list)
    for idx, r in enumerate(rows):
        groups[r[order_col]].append(idx)
    multi = [g for g in groups.values() if len(g) > 1]
    if not multi:
        return rows, stats

    nd_ = ndata if ndata is not None else len(header)
    # an order-level column is constant within (almost) every order group
    for i in range(nd_):
        if i == order_col or LINE_NO_RX.match(N.nd(header[i]).replace("_", " ")):
            continue
        ok = bad = 0
        for g in multi[:3000]:
            vals = {str(rows[j][i]) for j in g if rows[j][i] is not None}
            if len(vals) <= 1:
                ok += 1
            else:
                bad += 1
        if ok + bad and ok / (ok + bad) >= 0.99:
            stats["order_level_cols"].append(header[i])
            for g in multi:
                vals = [rows[j][i] for j in g if rows[j][i] is not None]
                if not vals or len(g) == len([1 for j in g if rows[j][i] is not None]):
                    continue
                fill = vals[0]
                for j in g:
                    if rows[j][i] is None:
                        rows[j][i] = fill
                        stats["carried"] += 1

    has_line_no = any(LINE_NO_RX.match(N.nd(h).replace("_", " ")) for h in header)
    if not has_line_no:
        keep, seen = [], set()
        for idx, r in enumerate(rows):
            k = (r[order_col], tuple(map(str, r[:nd_])))
            if k in seen:
                stats["dup_dropped"] += 1
                continue
            seen.add(k)
            keep.append(r)
        rows = keep
    return rows, stats


# --------------------------------------------------------------------------- #
def _logical_tables(dirpath: str, info: RM.ReadmeInfo):
    """Read the files and group same-header tables, keeping raw rows in memory."""
    groups = group_tables(RD.read_business(dirpath, info))
    for tname, ts in groups:
        header = ts[0].header
        rows, meta = [], []
        for t in ts:
            docs = info.docs_for(t.file, t.sheet)
            for ri, r in enumerate(t.rows):
                rows.append(list(r))
                meta.append((t.file, t.sheet, t.year_hint, t.month_hint, ri))
        names, used = [], set()
        for h in header:
            names.append(_uniq(ascii_name(h), used))
        docs = {}
        for t in ts:
            docs.update(info.docs_for(t.file, t.sheet))
        yield tname, header, names, rows, meta, docs, ts


def ingest_business(dirpath: str, outdir: str, verbose: bool = False,
                    classify_fn: CL.ClassifyFn = CL.heuristic,
                    provenance: str = "heuristic",
                    reuse_plan: bool = True) -> dict:
    """Load one business into SQLite, keeping its own tables and column names.

    Pass 1 takes a census of every column; a classifier then decides each
    column's kind; pass 2 parses the cells with the pure parser for that kind.
    """
    bid = os.path.basename(dirpath.rstrip("/"))
    t0 = time.time()
    info = RM.parse(os.path.join(dirpath, "README.md"))
    tables = list(_logical_tables(dirpath, info))

    # ---- pass 1: evidence ------------------------------------------------- #
    censuses = []
    for tname, header, names, rows, meta, docs, _ts in tables:
        yh = meta[0][2] if meta else None
        for i, (h, sqlname) in enumerate(zip(header, names)):
            censuses.append(C.census(tname, sqlname, h, [r[i] for r in rows],
                                     yh, docs.get(h)))

    # ---- decide the kinds ------------------------------------------------- #
    cached = CL.load_plan(outdir, bid) if reuse_plan else None
    if cached and set(cached[0]) >= {CL.key(c) for c in censuses}:
        plan, provenance = cached[0], cached[1] + " (cached)"
    else:
        plan = classify_fn(censuses)
        CL.save_plan(outdir, bid, plan, censuses, provenance)

    os.makedirs(os.path.join(outdir, bid), exist_ok=True)
    db_path = os.path.join(outdir, bid, "data.sqlite")
    if os.path.exists(db_path):
        os.remove(db_path)
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")

    profile = {
        "business_id": bid,
        "source_dir": dirpath,
        "description": info.description,
        "data_end": info.data_end,
        "kind_provenance": provenance,
        "readme_conventions": info.conventions,
        "conversions": [vars(c) for c in info.conversions],
        "tables": [],
    }

    # ---- pass 2: parse with the decided kinds ----------------------------- #
    for tname, header, names, rows, meta, docs, ts in tables:
        col_kinds = [plan.get(f"{tname}.{n}", "text") for n in names]

        cols, used, expanded = [], set(names), set()
        for h, base, kind in zip(header, names, col_kinds):
            cols.append((base, K.SQL_TYPE[kind], h, kind))
            for suf in K.EXPANSION[kind][1:]:
                expanded.add(len(cols))
                cols.append((_uniq(base + suf, used), K.SUFFIX_TYPE[suf],
                             h + K.SUFFIX_LABEL[suf], K.SUFFIX_KIND[suf]))

        parsed = []
        for r, m in zip(rows, meta):
            out = []
            for i, kind in enumerate(col_kinds):
                out.extend(K.parse(kind, r[i], m[2]))
            out += [m[0], m[1], m[2], m[3]]
            parsed.append(out)

        # every datetime column gets _time/_hour; a date-only one would leave them
        # NULL throughout, which only clutters the schema the model reads
        drop = {i for i in expanded if all(r[i] is None for r in parsed)}
        if drop:
            cols = [c for i, c in enumerate(cols) if i not in drop]
            parsed = [[v for i, v in enumerate(r) if i not in drop] for r in parsed]

        sql_names = [c[0] for c in cols]
        ocol = find_order_col(sql_names, [c[3] for c in cols], parsed)
        parsed, st = apply_order_rules(sql_names, [c[3] for c in cols], parsed, ocol,
                                       ndata=len(cols),
                                       parts=[(m[0], m[1]) for m in meta])

        meta_cols = [("__src_file", "TEXT"), ("__src_sheet", "TEXT"),
                     ("__year_hint", "INTEGER"), ("__month_hint", "INTEGER")]
        ddl = ", ".join(f'"{n}" {ty}' for n, ty, _o, _k in cols) + ", " + \
              ", ".join(f'"{n}" {ty}' for n, ty in meta_cols)
        con.execute(f'CREATE TABLE "{tname}" ({ddl})')
        ph = ",".join("?" * (len(cols) + len(meta_cols)))
        con.executemany(f'INSERT INTO "{tname}" VALUES ({ph})', parsed)

        # per-source-column statistics, under the kind that was decided
        yh = meta[0][2] if meta else None
        by_col = {}
        for i, (h, base, kind) in enumerate(zip(header, names, col_kinds)):
            by_col[base] = C.stats([r[i] for r in rows], kind, yh)

        profile["tables"].append({
            "name": tname,
            "rows": len(parsed),
            "sources": sorted({(t.file + ("::" + t.sheet if t.sheet else ""))
                               for t in ts})[:40],
            "n_sources": len(ts),
            "order_id_column": sql_names[ocol] if ocol is not None else None,
            "distinct_orders": (len({str(r[ocol]) for r in parsed})
                                if ocol is not None else None),
            "rows_per_order": (round(len(parsed) / max(1, len({str(r[ocol])
                               for r in parsed})), 3) if ocol is not None else None),
            "order_level_columns": st["order_level_cols"],
            "cells_carried_forward": st["carried"],
            "order_ids_filled": st["order_ids_filled"],
            "duplicate_rows_dropped": st["dup_dropped"],
            "columns": [
                {"sql": n, "original": orig, "kind": k,
                 "nulls": None, "distinct": None, "min": None, "max": None,
                 "samples": []}
                for n, _ty, orig, k in cols],
        })
        for ci, cd in enumerate(profile["tables"][-1]["columns"]):
            sc = by_col.get(cd["sql"])
            if sc is not None:                   # a source column: full stats
                cd["nulls"] = sc.n_blank
                cd["distinct"] = sc.distinct
                cd["min"] = sc.vmin
                cd["max"] = sc.vmax
                cd["samples"] = [str(x)[:60] for x in sc.samples]
                cd["parse_rate"] = round(sc.parse_rate, 5)
                cd["parse_failures"] = dict(sc.failures.most_common(8))
                continue
            # an expanded column (_time/_hour): describe its values
            vals = [r[ci] for r in parsed if r[ci] is not None]
            cd["nulls"] = len(parsed) - len(vals)
            cd["distinct"] = len({str(v) for v in vals})
            if vals:
                try:
                    cd["min"], cd["max"] = min(vals), max(vals)
                except TypeError:
                    pass
                cd["samples"] = [str(v) for v, _ in
                                 Counter(str(x) for x in vals).most_common(5)]

    con.commit()
    con.close()
    profile["ingest_seconds"] = round(time.time() - t0, 1)
    with open(os.path.join(outdir, bid, "profile.json"), "w", encoding="utf-8") as fh:
        json.dump(profile, fh, ensure_ascii=False, indent=1)
    if verbose:
        print(f"{bid:10s} {len(tables):2d} tables  "
              f"{sum(t['rows'] for t in profile['tables']):8d} rows  "
              f"kinds={provenance:22s} {profile['ingest_seconds']:6.1f}s", flush=True)
    return profile


def main(argv):
    roots = [a for a in argv if not a.startswith("--")]
    outdir = next((a.split("=", 1)[1] for a in argv if a.startswith("--out=")), "build")
    reuse = "--reclassify" not in argv
    classify_fn, provenance = CL.heuristic, "heuristic"
    if "--llm" in argv:                  # the model decides kinds (datamate/llm.py)
        from .. import llm as LLM
        classify_fn = CL.llm(lambda p, s: LLM.ask(p, schema=s, max_tokens=8192))
        provenance = f"llm:{LLM.MODEL}"
    roots = roots or ["train/data", "public_test/data"]
    for root in roots:
        for b in sorted(os.listdir(root)):
            p = os.path.join(root, b)
            if os.path.isdir(p):
                ingest_business(p, outdir, verbose=True, classify_fn=classify_fn,
                                provenance=provenance, reuse_plan=reuse)
