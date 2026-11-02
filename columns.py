"""Column evidence, and statistics once a kind has been decided.

Two jobs, deliberately separate:

  census()    gather the evidence a classifier needs about a column - its
              label, what its values look like, and how many of them each
              candidate kind could parse. No decision is made here.
  stats()     given a decided kind, parse the column and record ranges,
              distinct counts and anything that failed to parse.

`guess_kind()` is a deterministic FALLBACK classifier used when no better one
is supplied. It reads column names, which is exactly the brittle part: money
versus a count, a date column spelled "NgayBan", and a phone-number column that
looks numeric all defeat name patterns. Prefer a real classifier
(see `classify.py`).
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from .. import normalize as N
from . import kinds as K

MAX_PROBE = 4000


@dataclass
class Census:
    """What a classifier is shown about one column."""
    table: str
    column: str                              # SQL name
    original: str                            # the shop's own label
    n: int = 0
    n_blank: int = 0
    distinct: int = 0
    samples: list = field(default_factory=list)      # most common raw values
    rates: dict = field(default_factory=dict)        # kind -> parse rate
    has_time: bool = False
    documented: str | None = None                    # README description

    @property
    def n_value(self) -> int:
        return self.n - self.n_blank


def census(table: str, column: str, original: str, values: list,
           year_hint: int | None = None, documented: str | None = None) -> Census:
    c = Census(table=table, column=column, original=original, n=len(values),
               documented=documented)
    vals = [v for v in values if not N.is_blank(v)]
    c.n_blank = len(values) - len(vals)
    if not vals:
        c.rates = {"empty": 1.0}
        return c

    probe = vals if len(vals) <= MAX_PROBE else vals[::max(1, len(vals) // MAX_PROBE)]
    counts = {k: 0 for k in ("date", "datetime", "number", "bool")}
    n_time = n_date_strict = 0
    for v in probe:
        d, t = N.parse_datetime(v, year_hint)
        if d:
            counts["date"] += 1
            if t:
                n_time += 1
            if not isinstance(v, (int, float)) or hasattr(v, "year"):
                n_date_strict += 1     # a real date string, not a bare serial
        if N.parse_number(v) is not None:
            counts["number"] += 1
        if N.parse_bool(v) is not None:
            counts["bool"] += 1

    m = len(probe)
    c.rates = {k: round(v / m, 4) for k, v in counts.items()}
    c.rates["datetime"] = round(n_time / m, 4)
    c.rates["date_strict"] = round(n_date_strict / m, 4)
    c.has_time = (n_time / max(1, counts["date"])) >= 0.5
    c.distinct = len({str(v) for v in probe})
    c.samples = [v for v, _ in Counter(str(v) for v in probe).most_common(8)]
    return c


# --------------------------------------------------------------------------- #
# fallback classifier - name patterns, which is the brittle part
# --------------------------------------------------------------------------- #
DATE_HINT = re.compile(r"ngay|date|han|thoi gian|thoigian|ngaygio|sinh nhat|hsd|"
                       r"han dung|han su dung|ngay gio|tao luc|ngaylap|ngaytt|"
                       r"expiry|vao lam|nhan viec|thoi viec|khai truong|ap dung|"
                       r"dang ky", re.I)
MONEY_HINT = re.compile(r"tien|\bgia\b|thanh toan|\bno\b|tra truoc|da tra|khach tra|"
                        r"\bthu\b|\bchi\b|chiet ?khau|\bck\b|giam|\bphi\b|ship|von|"
                        r"\btong\b|amount|price|cost|bill|tri ?gia|da gop|gop", re.I)


def guess_kind(c: Census) -> str:
    """Deterministic fallback: decide a kind from the census alone."""
    if not c.n_value:
        return "empty"
    key = N.nd(c.column).replace("_", " ")
    r = c.rates
    if (DATE_HINT.search(key) and r.get("date", 0) >= 0.80) \
            or r.get("date_strict", 0) >= 0.95:
        return "datetime" if c.has_time else "date"
    # only plain numbers: a quantity written with its unit ("2 ly") is text
    if r.get("number", 0) >= 0.95:
        return "money" if MONEY_HINT.search(key) else "number"
    if r.get("bool", 0) >= 0.98 and c.distinct <= 4:
        return "bool"
    return "text"


# --------------------------------------------------------------------------- #
@dataclass
class ColumnStats:
    kind: str
    n: int = 0
    n_blank: int = 0
    n_parsed: int = 0
    distinct: int = 0
    samples: list = field(default_factory=list)
    failures: Counter = field(default_factory=Counter)
    vmin: object = None
    vmax: object = None

    @property
    def n_value(self) -> int:
        return self.n - self.n_blank

    @property
    def parse_rate(self) -> float:
        return self.n_parsed / self.n_value if self.n_value else 1.0


def stats(values: list, kind: str, year_hint: int | None = None) -> ColumnStats:
    """Parse a column with its decided kind and record what came out."""
    st = ColumnStats(kind=kind, n=len(values))
    parsed = []
    for v in values:
        if N.is_blank(v):
            st.n_blank += 1
            continue
        pv = K.parse(kind, v, year_hint)[0]
        if pv is None and kind != "empty":
            st.failures[str(v)[:60]] += 1
        else:
            st.n_parsed += 1
            if pv is not None:
                parsed.append(pv)
    st.distinct = len({str(v) for v in parsed})
    if kind in ("date", "datetime", "money", "number") and parsed:
        try:
            st.vmin, st.vmax = min(parsed), max(parsed)
        except TypeError:
            pass
    st.samples = [v for v, _ in Counter(str(v) for v in parsed).most_common(8)]
    return st
