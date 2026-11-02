"""Parse a business README.md.

The READMEs are machine-regular across all 44 businesses: `## File`,
`## Cot trong <file>` (optionally `, sheet <sheet>`), `## Bang`, `## Quy uoc`,
and every one of them states its data-end date. We lean on that rather than
hard-coding anything per business (rule 4).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

BOLD = re.compile(r"\*\*(.+?)\*\*")
TICK = re.compile(r"`([^`]+)`")
DATE_END = re.compile(r"[Dd]ữ liệu có đến hết\s*(\d{1,2})/(\d{1,2})/(\d{4})")
# "  - Xi mang Ha Tien PCB40 bao 50kg: 1 tan = 20 bao"
CONV = re.compile(r"^\s*-\s*(?P<item>.+?):\s*1\s*(?P<big>.+?)\s*=\s*(?P<f>[\d.,]+)\s*(?P<base>.+?)\s*$")
HDR = re.compile(r"^##\s*(?P<kind>.+?)\s*$")


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


@dataclass
class Conversion:
    item: str
    big_unit: str
    factor: float
    base_unit: str


@dataclass
class ReadmeInfo:
    text: str = ""
    description: str = ""
    files: list[str] = field(default_factory=list)
    # (file, sheet|None) -> documented column names, in declared order
    columns: dict[tuple[str, str | None], list[str]] = field(default_factory=dict)
    # (file, sheet) -> {column label: the README's description of it}
    column_docs: dict[tuple[str, str | None], dict[str, str]] = field(default_factory=dict)
    tables: list[str] = field(default_factory=list)      # sqlite table names
    conversions: list[Conversion] = field(default_factory=list)
    data_end: str | None = None                          # ISO date
    conventions: list[str] = field(default_factory=list)  # "Quy uoc" bullet lines

    def docs_for(self, filename: str, sheet: str | None = None) -> dict[str, str]:
        """What the README says each column of this file means."""
        for key in ((filename, sheet), (filename, None)):
            if key in self.column_docs:
                return self.column_docs[key]
        for (f, _s), docs in self.column_docs.items():
            if f == filename:
                return docs
        return {}

    def columns_for(self, filename: str, sheet: str | None = None) -> list[str]:
        """Documented columns for a file, preferring an exact sheet match."""
        if (filename, sheet) in self.columns:
            return self.columns[(filename, sheet)]
        if (filename, None) in self.columns:
            return self.columns[(filename, None)]
        # some READMEs document one sheet and the rest share its columns
        for (f, _s), cols in self.columns.items():
            if f == filename:
                return cols
        return []

    def all_documented_columns(self) -> list[str]:
        out: list[str] = []
        for cols in self.columns.values():
            out.extend(cols)
        return out


def parse(path: str) -> ReadmeInfo:
    with open(path, encoding="utf-8") as fh:
        text = nfc(fh.read())
    info = ReadmeInfo(text=text)

    section = None
    cur_key: tuple[str, str | None] | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        m = HDR.match(line)
        if m:
            kind = m.group("kind")
            cur_key = None
            if kind.startswith("File"):
                section = "files"
            elif kind.startswith("Bảng"):
                section = "tables"
            elif kind.startswith("Quy ước"):
                section = "conventions"
            elif kind.startswith("Cột trong"):
                section = "columns"
                ticks = TICK.findall(kind)
                fname = ticks[0] if ticks else ""
                sheet = ticks[1] if len(ticks) > 1 else None
                cur_key = (fname, sheet)
                info.columns.setdefault(cur_key, [])
                info.column_docs.setdefault(cur_key, {})
            else:
                section = None
            continue

        if not line.strip():
            continue

        if section == "files":
            for t in TICK.findall(line):
                if t not in info.files:
                    info.files.append(t)
        elif section == "tables":
            for t in TICK.findall(line):
                if t not in info.tables:
                    info.tables.append(t)
        elif section == "columns" and cur_key is not None:
            b = BOLD.findall(line)
            if b and line.lstrip().startswith("-"):
                name = b[0].strip()
                info.columns[cur_key].append(name)
                desc = line.split("**:", 1)[-1] if "**:" in line else ""
                desc = desc.strip() or line.split(":", 1)[-1].strip()
                if desc and desc != line.strip():
                    info.column_docs[cur_key][name] = desc
        elif section == "conventions":
            info.conventions.append(line.strip())
            c = CONV.match(line)
            if c and not BOLD.search(line):
                try:
                    f = float(c.group("f").replace(".", "").replace(",", ".")) \
                        if ("." in c.group("f") or "," in c.group("f")) else float(c.group("f"))
                except ValueError:
                    f = None
                if f:
                    info.conversions.append(Conversion(
                        c.group("item").strip(), c.group("big").strip(), f,
                        c.group("base").strip()))
        elif section is None and not info.description and not line.startswith("#"):
            info.description = line.strip()

    m = DATE_END.search(text)
    if m:
        d, mo, y = (int(x) for x in m.groups())
        info.data_end = f"{y:04d}-{mo:02d}-{d:02d}"

    # files mentioned only in the "File" bullets but with parenthetical notes
    info.files = [f for f in info.files if re.search(r"\.(csv|xlsx|sqlite)$", f, re.I)]
    return info
