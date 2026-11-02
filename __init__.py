"""Phase 1 - a business folder of messy files -> one SQLite db describing itself.

Self-contained: nothing outside this package reads the source files, and the
only thing it hands downstream is what it writes to disk, per business, in
`<out>/<business_id>/`:

    data.sqlite    the shop's own tables, cells parsed to typed values
    profile.json   schema, decided kinds, value ranges, samples, parse rates
    kinds.json     what each column was decided to mean, and by whom

    parse_readme.py   parse a business README (columns, conventions, conversions)
    readers.py        csv / xlsx / sqlite -> raw tables; headers; non-record rows
    columns.py        column evidence (census), stats, the heuristic fallback
    classify.py       decide each column's kind - pluggable; the LLM goes here
    kinds.py          the closed kind vocabulary + parse(kind, raw) dispatch
    pipeline.py       the two passes, rulebook s8 row rules, the SQLite writer
    report.py         coverage over the cached profiles

The pure value parsers live outside, in `datamate.normalize`, because the query
step reads the shop's own labels back out of the cells at answer time.

    python -m datamate.ingest train/data public_test/data --out=build
    python -m datamate.ingest.report --failures
"""
from __future__ import annotations

from .pipeline import ingest_business, main

__all__ = ["ingest_business", "main"]
