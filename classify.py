"""Decide each column's kind. This is the pluggable, semantic step.

Which kind a column holds is a judgement about meaning - "NgayBan" is a date
even when its cells are Excel serials, "Tiền" is money while "Số lượng" is a
count, "Biển số xe" and "SĐT" are text even though they are mostly digits. Name patterns get that wrong often enough to matter, so the
decision is a function you can replace:

    ClassifyFn = (list[Census]) -> {"table.column": kind}

`heuristic` is the offline default. `llm` is the intended one: `prompt(...)`
renders the evidence, `schema(...)` constrains the reply to one valid kind per
column, and `from_response` parses it back into a plan.

The decision is cached in `build/<id>/kinds.json`, so re-ingesting does not
re-ask, and two runs of the same image ingest identically - which the
reproducibility rule needs.
"""
from __future__ import annotations

import json
import os
from typing import Callable, Iterable

from .columns import Census, guess_kind
from .kinds import KIND_HELP, KINDS

ClassifyFn = Callable[[list], dict]


def key(c: Census) -> str:
    return f"{c.table}.{c.column}"


# --------------------------------------------------------------------------- #
def heuristic(censuses: Iterable[Census]) -> dict:
    """Offline default: the name-and-parse-rate fallback in columns.py."""
    return {key(c): guess_kind(c) for c in censuses}


# --------------------------------------------------------------------------- #
#: what the model chooses from. `empty` is a fact about the data, not a meaning,
#: so it is decided in code and empty columns are never sent.
ASKED = tuple(k for k in KINDS if k != "empty")

#: share of a column's values that must parse as plain numbers for it to stay
#: number/money. Mixed quantity columns ("1 | 2 ly | 1 bình") sit at 0.5-0.65.
PLAIN_NUMBER_SHARE = 0.95


def prompt(censuses: Iterable[Census]) -> str:
    """The question for a model: kind definitions, then one line per column
    (name, the shop's label, the README's description, a few raw values)."""
    lines = ["Classify every column of a shop's data by what the column means.", ""]
    lines += [f"{k:9s} {KIND_HELP[k]}" for k in ASKED]
    table = None
    for c in censuses:
        if c.table != table:
            table = c.table
            lines += ["", f"TABLE {table}"]
        line = f'{key(c)}  "{c.original}"'
        if c.documented:
            line += f"  ({c.documented})"
        eg = " | ".join(str(s)[:30] for s in c.samples[:5])
        lines.append(f"{line}  eg: {eg}")
    return "\n".join(lines)


def schema(censuses: Iterable[Census]) -> dict:
    """JSON schema for the reply: every column once, each a valid kind. Served
    as a structured-output constraint, so the reply cannot omit a column or
    invent a kind."""
    keys = [key(c) for c in censuses]
    return {"type": "object", "additionalProperties": False, "required": keys,
            "properties": {k: {"type": "string", "enum": list(ASKED)} for k in keys}}


def from_response(text: str, censuses: Iterable[Census]) -> dict:
    """Parse a model reply into a plan. Empty columns are empty; anything the
    reply lacks or gets wrong falls back to the heuristic, per column."""
    cs = list(censuses)
    plan = heuristic(cs)
    try:
        got = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        got = {}
    for c in cs:
        v = got.get(key(c))
        if c.n_value == 0:
            plan[key(c)] = "empty"
        elif isinstance(v, str) and v in ASKED:
            plan[key(c)] = v
        # number/money also needs the values to BE numbers - a fact, checked
        # here: the model sees only the commonest values, and a quantity column
        # whose other half reads "2 ly", "1 bình" looks numeric from those
        if plan[key(c)] in ("number", "money") and \
                c.rates.get("number", 0) < PLAIN_NUMBER_SHARE:
            plan[key(c)] = "text"
    return plan


def llm(ask: Callable[[str, dict], str]) -> ClassifyFn:
    """Wrap a model call `ask(prompt, schema) -> reply text` into a ClassifyFn."""
    def classify(censuses):
        cs = list(censuses)
        asked = [c for c in cs if c.n_value]
        reply = ask(prompt(asked), schema(asked)) if asked else ""
        return from_response(reply, cs)
    return classify


# --------------------------------------------------------------------------- #
def plan_path(outdir: str, bid: str) -> str:
    return os.path.join(outdir, bid, "kinds.json")


def load_plan(outdir: str, bid: str) -> tuple[dict, str] | None:
    path = plan_path(outdir, bid)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    return {k: v["kind"] for k, v in doc["columns"].items()}, doc.get("provenance", "?")


def save_plan(outdir: str, bid: str, plan: dict, censuses: Iterable[Census],
              provenance: str) -> None:
    os.makedirs(os.path.join(outdir, bid), exist_ok=True)
    doc = {
        "business_id": bid,
        "provenance": provenance,
        "columns": {
            key(c): {"kind": plan.get(key(c), "text"), "label": c.original,
                     "readme": c.documented, "samples": c.samples[:5]}
            for c in censuses
        },
    }
    with open(plan_path(outdir, bid), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)
