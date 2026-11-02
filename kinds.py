"""The closed set of value kinds, and the pure parser for each.

A kind says what a column *means*; deciding that is a semantic judgement and
belongs to the classifier (see `classify.py`). Turning a raw cell into a value
once the kind is known is mechanical, and that is all this module does: given
`(kind, raw)` it returns the value(s), with no reference to the column's name.

Keep these pure. Anything that needs to look at a column name, its neighbours
or a README belongs in the classifier, not here.
"""
from __future__ import annotations

from .. import normalize as N

#: every kind a column may be assigned. A classifier must return one of these.
#: A kind is what the column MEANS, not what its cells happen to parse as.
KINDS = ("money", "date", "datetime", "bool", "text", "empty", "number")

KIND_HELP = {
    "money": "an amount of money (VND), whatever its size and even when written as a"
             " plain number (800): price (giá), cost (vốn), total (tổng tiền),"
             " discount, fee, debt, payment",
    "date": "a calendar day: order date, due date, expiry, birthday - even when"
            " written as an Excel serial (45850)",
    "datetime": "a moment: a date with a clock time",
    "bool": "a yes/no flag, often 1/0 or True/False: whether something is allowed,"
            " active, required",
    "text": "names, codes, IDs, phone numbers, categories, payment methods, units,"
            " states, free notes - and any column whose values mix numbers with"
            " words ('2 ly', '10 vỉ')",
    "empty": "the column holds no values at all",
    "number": "a quantity that is not money - a count, an amount of goods, a ratio,"
              " a percentage, a conversion factor, a row number - written as plain"
              " numbers",
}

#: how many SQL columns each kind expands to, and the suffixes used. An
#: expansion that comes out entirely NULL is dropped at ingest.
EXPANSION = {
    "money": ("",),
    "date": ("",),
    "datetime": ("", "_time", "_hour"),
    "bool": ("",),
    "text": ("",),
    "empty": ("",),
    "number": ("",),
}

SQL_TYPE = {"money": "REAL", "date": "TEXT", "datetime": "TEXT", "bool": "INTEGER",
            "text": "TEXT", "empty": "TEXT", "number": "REAL"}

SUFFIX_TYPE = {"_time": "TEXT", "_hour": "INTEGER"}
SUFFIX_LABEL = {"_time": " (gio)", "_hour": " (gio nguyen)"}
#: the kind reported for an expanded column, so the profile describes it truly
SUFFIX_KIND = {"_time": "text", "_hour": "number"}


def parse(kind: str, raw, year_hint: int | None = None) -> list:
    """(kind, raw cell) -> the value(s) for that kind's SQL column(s).

    Pure: the same inputs always give the same output, and nothing about the
    column other than its kind is consulted.
    """
    if kind == "date":
        return [N.parse_date(raw, year_hint)]
    if kind == "datetime":
        d, t = N.parse_datetime(raw, year_hint)
        return [d, t, int(t[:2]) if t else None]
    if kind in ("money", "number"):
        return [N.parse_number(raw)]
    if kind == "bool":
        b = N.parse_bool(raw)
        return [None if b is None else int(b)]
    return [N.norm_name(raw)]            # text, empty


def parsed_ok(kind: str, raw, year_hint: int | None = None) -> bool:
    """Did the raw cell yield a value? Blank cells never count as failures."""
    if N.is_blank(raw):
        return True
    return parse(kind, raw, year_hint)[0] is not None


def has_time(raw, year_hint: int | None = None) -> bool:
    return N.parse_datetime(raw, year_hint)[1] is not None
