"""Read raw tables out of .csv / .xlsx / .sqlite into one uniform shape.

Two jobs that are easy to get wrong and expensive to get wrong:
  * header detection - the hand-kept Excel books put title rows above the
    header ("SO CHI TIET BAN HANG", "Ky bao cao: toan bo", blank, then header).
    We score candidate rows against the column names the README documents.
  * period hints - a sheet or file named T02-2025 / Thang01_2025 / BanHang_2026
    is the year a bare "12/03" date in that sheet belongs to (rulebook s8).
"""
from __future__ import annotations

import csv
import os
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime

from .readme import ReadmeInfo, nfc

MAX_HEADER_SCAN = 12

# Hand-kept books end each month with a "Tong thang" row whose money cell holds
# the monthly sum, and sprinkle "Ghi chu: ..." annotation rows. Counting those as
# records doubled one shop's revenue exactly. Exported CSVs sometimes repeat the
# header mid-file too.
# These MUST keep their diacritics. Stripped to ASCII, the Vietnamese surname
# "Tong Thang Ngan" is indistinguishable from the label "Tong thang" (monthly
# total), and an ASCII rule deleted real customers' order lines.
TOTAL_RX = re.compile(r"^(?:tổng|cộng|total|sum|lũy kế|ghi chú|chú thích|note|"
                      r"thống kê|tiêu đề|kết thúc|subtotal)\b")
TOTAL_EXACT = {"tong", "cong", "tong cong", "total", "sum", "subtotal", "luy ke"}
# An explicit running-total label ("Cong ngay 01/05/2025", "Tong thang 3"). These
# rows are otherwise fully populated - one shop put them in the customer column
# and reused the previous bill's code, which inflated its revenue 2x.
RUNNING_TOTAL_RX = re.compile(r"^(?:tổng|cộng|lũy kế|tích lũy)\s+"
                              r"(?:ngày|tháng|tuần|năm|quý|cộng|số)\b")


@dataclass
class RawTable:
    key: str
    file: str
    sheet: str | None
    header: list[str]
    rows: list[list] = field(default_factory=list)
    year_hint: int | None = None
    month_hint: int | None = None
    header_row: int = 0
    source_kind: str = "csv"

    @property
    def ncol(self) -> int:
        return len(self.header)

    def dicts(self):
        for r in self.rows:
            yield dict(zip(self.header, r))


# --------------------------------------------------------------------------- #
# period hints from file / sheet names
# --------------------------------------------------------------------------- #
_MY = re.compile(r"(?:^|[^0-9])(?:t|thang|th|tháng)?\s*(\d{1,2})\s*[-_/.]\s*((?:19|20)\d{2})(?:$|[^0-9])", re.I)
_YM = re.compile(r"(?:^|[^0-9])((?:19|20)\d{2})\s*[-_/.]\s*(\d{1,2})(?:$|[^0-9])")
_Y = re.compile(r"(?:^|[^0-9])((?:19|20)\d{2})(?:$|[^0-9])")


def period_hint(*names: str) -> tuple[int | None, int | None]:
    """(year, month) implied by a sheet/file name, month optional."""
    year = month = None
    for name in names:
        if not name:
            continue
        s = unicodedata.normalize("NFC", name)
        s = re.sub(r"\.(csv|xlsx|sqlite)$", "", s, flags=re.I)
        m = _MY.search(s)
        if m and 1 <= int(m.group(1)) <= 12:
            return int(m.group(2)), int(m.group(1))
        m = _YM.search(s)
        if m and 1 <= int(m.group(2)) <= 12:
            return int(m.group(1)), int(m.group(2))
        m = _Y.search(s)
        if m and year is None:
            year = int(m.group(1))
    return year, month


# --------------------------------------------------------------------------- #
# header detection
# --------------------------------------------------------------------------- #
def _key(s) -> str:
    s = unicodedata.normalize("NFD", str(s).lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", "", s)


def _words(s) -> str:
    """Lowercase and whitespace-collapse, KEEPING diacritics (see TOTAL_RX)."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", str(s)).lower()).strip()


def _ascii_words(s) -> str:
    s = unicodedata.normalize("NFD", str(s).lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s.replace("đ", "d")).strip()


def _is_junk_row(cells: list, header_keys: set[str], width: int) -> bool:
    filled = [c for c in cells if c is not None and str(c).strip() != ""]
    if not filled:
        return True
    keys = {_key(c) for c in filled}
    if len(filled) >= 2 and keys and keys <= header_keys:
        return True                        # the header repeated inside the body
    for c in filled:
        if RUNNING_TOTAL_RX.match(_words(c)):
            return True                    # a running-total row, however full
    if len(filled) <= max(1, int(0.6 * width)):
        for c in filled:
            w = _words(c)
            if TOTAL_RX.match(w) or _ascii_words(c) in TOTAL_EXACT:
                return True                # a total / annotation row
    return False


def _looks_like_label(v) -> bool:
    if not isinstance(v, str):
        return False
    s = v.strip()
    if not s or len(s) > 60:
        return False
    if re.fullmatch(r"[-+]?[\d\s.,]+%?đ?", s):   # a number, not a label
        return False
    return True


def _score_header(row: list, doc_keys: set[str], below: list[list]) -> float:
    cells = [c for c in row if c is not None and str(c).strip() != ""]
    if len(cells) < 2:
        return -1e9
    labels = sum(1 for c in cells if _looks_like_label(c))
    if labels < 2:
        return -1e9
    hit = sum(1 for c in cells if _key(c) in doc_keys)
    score = 12.0 * hit + 2.0 * labels
    score -= 3.0 * (len(cells) - labels)           # data-looking cells
    if len({_key(c) for c in cells}) < len(cells):  # duplicate labels
        score -= 4.0
    widths = [len([c for c in r if c is not None and str(c).strip() != ""])
              for r in below[:8]]
    if widths:
        avg = sum(widths) / len(widths)
        score -= 1.5 * abs(avg - len(cells))        # header should match body width
    return score


def detect_header(rows: list[list], doc_cols: list[str]) -> int:
    doc_keys = {_key(c) for c in doc_cols if str(c).strip()}
    best, best_s = 0, -1e18
    for i in range(min(MAX_HEADER_SCAN, len(rows))):
        s = _score_header(rows[i], doc_keys, rows[i + 1:])
        if s > best_s:
            best, best_s = i, s
    return best


def _mk_header(row: list, width: int) -> list[str]:
    out, seen = [], {}
    for i in range(width):
        v = row[i] if i < len(row) else None
        name = nfc(str(v)).strip() if v is not None and str(v).strip() != "" else f"col{i + 1}"
        name = re.sub(r"\s+", " ", name)
        if name in seen:
            seen[name] += 1
            name = f"{name}__{seen[name]}"
        else:
            seen[name] = 1
        out.append(name)
    return out


def _finish(key, fname, sheet, rows, doc_cols, kind) -> RawTable | None:
    rows = [r for r in rows if any(c is not None and str(c).strip() != "" for c in r)]
    if not rows:
        return None
    hi = detect_header(rows, doc_cols)
    body = rows[hi + 1:]
    width = max([len(rows[hi])] + [len(r) for r in body[:200]]) if body else len(rows[hi])
    # trailing all-empty columns
    while width > 1 and all((len(r) < width or r[width - 1] is None or
                             str(r[width - 1]).strip() == "") for r in [rows[hi]] + body[:200]):
        width -= 1
    header = _mk_header(rows[hi], width)
    header_keys = {_key(c) for c in rows[hi] if c is not None and str(c).strip() != ""}
    norm_body = []
    for r in body:
        cells = [(r[i] if i < len(r) else None) for i in range(width)]
        if _is_junk_row(cells, header_keys, width):
            continue
        norm_body.append(cells)
    y, m = period_hint(sheet or "", fname)
    return RawTable(key=key, file=fname, sheet=sheet, header=header, rows=norm_body,
                    year_hint=y, month_hint=m, header_row=hi, source_kind=kind)


# --------------------------------------------------------------------------- #
# per-format readers
# --------------------------------------------------------------------------- #
def read_csv(path: str, info: ReadmeInfo) -> list[RawTable]:
    fname = os.path.basename(path)
    with open(path, encoding="utf-8-sig", newline="") as fh:
        head = fh.read(8192)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(head, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        rows = [[(c if c != "" else None) for c in r] for r in csv.reader(fh, dialect)]
    t = _finish(fname, fname, None, rows, info.columns_for(fname), "csv")
    return [t] if t else []


def read_xlsx(path: str, info: ReadmeInfo) -> list[RawTable]:
    import openpyxl

    fname = os.path.basename(path)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out = []
    try:
        for sheet in wb.sheetnames:
            ws = wb[sheet]
            rows = [list(r) for r in ws.iter_rows(values_only=True)]
            t = _finish(f"{fname}::{sheet}", fname, sheet, rows,
                        info.columns_for(fname, sheet), "xlsx")
            if t:
                out.append(t)
    finally:
        wb.close()
    return out


def read_sqlite(path: str, info: ReadmeInfo) -> list[RawTable]:
    fname = os.path.basename(path)
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.text_factory = lambda b: b.decode("utf-8", "replace")
    out = []
    try:
        names = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for n in names:
            cur = con.execute(f'SELECT * FROM "{n}"')
            header = [d[0] for d in cur.description]
            rows = [list(r) for r in cur.fetchall()]
            y, m = period_hint(n, fname)
            out.append(RawTable(key=f"{fname}::{n}", file=fname, sheet=n,
                                header=header, rows=rows, year_hint=y, month_hint=m,
                                source_kind="sqlite"))
    finally:
        con.close()
    return out


def read_business(dirpath: str, info: ReadmeInfo) -> list[RawTable]:
    """Enumerate the directory (never the README file list - two businesses
    declare a glob pattern there instead of real names)."""
    out = []
    for fname in sorted(os.listdir(dirpath)):
        path = os.path.join(dirpath, fname)
        if not os.path.isfile(path) or fname == "README.md":
            continue
        ext = os.path.splitext(fname)[1].lower()
        if ext == ".csv":
            out += read_csv(path, info)
        elif ext in (".xlsx", ".xlsm"):
            out += read_xlsx(path, info)
        elif ext in (".sqlite", ".db", ".sqlite3"):
            out += read_sqlite(path, info)
    return out
