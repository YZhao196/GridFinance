"""Statement parsing, identity and the import pipeline (§19–§21).

Each stage is a separate function, which is what makes the pipeline testable end to end
from a fixture file with no bank and no network:

    file/sync ──▶ parse ──▶ normalise ──▶ identify ──▶ categorise ──▶ store

Normalisation produces one shape — ``date``, signed ``amount`` (negative is money out)
and ``description`` — and every parser produces that shape, so nothing downstream cares
where a row came from.

This module and :mod:`finance_tool.engine.sync` are the only two in the engine allowed
to touch the filesystem or a socket (§15).
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Sequence

from finance_tool.engine import categorise as cat
from finance_tool.engine import detect as det
from finance_tool.store.entities import Rule, Transaction
from finance_tool.store.store import StoreView, new_id

# --------------------------------------------------------------------------- shapes


@dataclass(frozen=True)
class RawTxn:
    """One normalised statement line. Negative amount is money out (§20)."""

    date: date
    amount: float
    description: str
    external_id: str | None = None
    raw: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SkippedRow:
    """A row that could not be read, kept so the count can be explained (§32)."""

    line: int
    reason: str
    text: str = ""


@dataclass(frozen=True)
class ColumnChoice:
    """Which column is which, and how confident the detection is."""

    date: int | None = None
    amount: int | None = None
    description: int | None = None
    debit: int | None = None
    credit: int | None = None
    balance: int | None = None
    indicator: int | None = None
    day_first: bool = True
    confidence: float = 0.0
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.date is not None and (self.amount is not None or self.debit is not None
                                          or self.credit is not None)


@dataclass(frozen=True)
class ParseResult:
    source: str
    rows: list[RawTxn] = field(default_factory=list)
    skipped: list[SkippedRow] = field(default_factory=list)
    columns: ColumnChoice = ColumnChoice()
    headers: list[str] = field(default_factory=list)
    header_row: int | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def read(self) -> int:
        return len(self.rows)

    @property
    def is_empty(self) -> bool:
        return not self.rows and not self.skipped

    def describe(self) -> str:
        """ "412 read, 7 skipped" — counted and shown, never silent (§32)."""
        text = f"{self.read} read"
        if self.skipped:
            text += f", {len(self.skipped)} skipped"
        return text


@dataclass(frozen=True)
class ImportSummary:
    """What an import did, in numbers the user can check."""

    source: str = ""
    read: int = 0
    skipped: int = 0
    imported: int = 0
    duplicates: int = 0
    categorised: int = 0
    uncategorised: int = 0
    skipped_samples: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    account: str = ""
    # The dates the new rows actually fall on, so a caller can reconcile exactly the
    # window it just imported rather than guessing one.
    span: tuple[date, date] | None = None

    @property
    def total(self) -> int:
        return self.read + self.skipped

    @property
    def is_empty(self) -> bool:
        return self.read == 0 and self.skipped == 0

    @property
    def coverage(self) -> float:
        if self.imported <= 0:
            return 1.0
        return self.categorised / self.imported

    def describe(self) -> str:
        parts = [f"{self.read} read"]
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        parts.append(f"{self.imported} new")
        if self.duplicates:
            parts.append(f"{self.duplicates} already had")
        return ", ".join(parts)


# ------------------------------------------------------------------- date parsing


DATE_FORMATS = (
    "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d", "%d %b %Y", "%d %B %Y",
    "%d-%b-%Y", "%d-%b-%y", "%b %d %Y", "%b %d, %Y", "%B %d, %Y",
)

_SPLIT_DATE = re.compile(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})$")


def _expand_year(year: int) -> int:
    if year >= 100:
        return year
    return 2000 + year if year < 70 else 1900 + year


def parse_date(text: str, *, day_first: bool = True) -> date | None:
    """A date from whatever the bank printed, or ``None``.

    The ambiguous ``dd/mm`` vs ``mm/dd`` case is decided by the caller's
    :func:`infer_date_order`, which reads the whole column rather than guessing per row —
    a statement where one row reads as April and the next as March would be worse than
    either convention applied consistently.
    """
    if not text:
        return None
    cleaned = str(text).strip().strip('"')
    if not cleaned:
        return None

    match = _SPLIT_DATE.match(cleaned)
    if match:
        first, second, year = (int(part) for part in match.groups())
        day, month = (first, second) if day_first else (second, first)
        if day > 31 or month > 12:
            day, month = month, day  # one of them is out of range; swap and re-check
        try:
            return date(_expand_year(year), month, day)
        except ValueError:
            return None

    for pattern in DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, pattern).date()
        except ValueError:
            continue
    return None


def infer_date_order(samples: Sequence[str], *, default_day_first: bool = True) -> bool:
    """Whether an ambiguous date column is day-first, read from the whole sample.

    A component above 12 settles it outright. With no such evidence the app's own locale
    decides, because inventing a rule per row is how a statement ends up half in April.
    """
    for text in samples:
        match = _SPLIT_DATE.match(str(text or "").strip())
        if not match:
            continue
        first, second = int(match.group(1)), int(match.group(2))
        if first > 12 and second <= 12:
            return True
        if second > 12 and first <= 12:
            return False
    return default_day_first


# ----------------------------------------------------------------- amount parsing


_CURRENCY = re.compile(r"[^\d.,\-()+ ]")


def parse_amount(text: str) -> float | None:
    """A signed amount from a bank cell, or ``None``.

    Handles the conventions banks actually use: thousands separators, a currency symbol,
    parentheses for negatives, a trailing minus, and an explicit ``+``.
    """
    if text is None:
        return None
    cleaned = str(text).strip().strip('"')
    if not cleaned:
        return None

    negative = False
    if cleaned.startswith("(") and cleaned.endswith(")"):
        negative = True
        cleaned = cleaned[1:-1]
    if cleaned.endswith("-"):
        negative = True
        cleaned = cleaned[:-1]

    cleaned = _CURRENCY.sub("", cleaned).replace(" ", "")
    if not cleaned or cleaned in {"-", "+"}:
        return None

    # Separator conventions. With both present the last one is the decimal point; with
    # only a comma, the size of its trailing group says which it is — "12,34" is a
    # decimal comma, "1,234" is a thousands separator. Where it is genuinely ambiguous
    # this returns a number rather than guessing a different one, and a format it cannot
    # read at all returns None so the row is counted as skipped rather than misread.
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        head, _, tail = cleaned.rpartition(",")
        if len(tail) == 3 and 1 <= len(head.lstrip("-+")) <= 3:
            cleaned = cleaned.replace(",", "")
        elif len(tail) <= 2:
            cleaned = head + "." + tail
        else:
            cleaned = cleaned.replace(",", "")

    try:
        value = float(cleaned)
    except ValueError:
        return None
    if negative:
        value = -abs(value)
    return value


def parse_indicator(text: str) -> int | None:
    """``-1`` for a debit marker, ``+1`` for a credit marker, ``None`` for neither."""
    token = str(text or "").strip().upper()
    if token in {"DR", "D", "DEBIT", "WITHDRAWAL", "PAYMENT", "-"}:
        return -1
    if token in {"CR", "C", "CREDIT", "DEPOSIT", "RECEIPT", "+"}:
        return 1
    return None


# A cell that is *entirely* a number: optional sign (or parentheses / trailing minus),
# optional currency symbol, digits with separators. Used when choosing columns, where
# "does this look like money" has to be strict — a permissive test scores any description
# with a number in it ("CAFE LATTE 0091") as an amount column.
_NUMERIC_CELL = re.compile(r"^\s*[-+(]?\s*[$€£¥]?\s*[\d.,]+\s*[)\-]?\s*$")


def looks_like_amount(text: str) -> bool:
    return bool(_NUMERIC_CELL.match(str(text or ""))) and parse_amount(text) is not None


# ------------------------------------------------------------ CSV column detection

# Header vocabulary. Matching is on a normalised header (lowercased, punctuation
# dropped), so "Transaction Date", "transaction_date" and "TRANSACTIONDATE" all land.
HINTS: dict[str, set[str]] = {
    "date": {"date", "transactiondate", "dateposted", "posted", "postdate", "valuedate",
             "effectivedate", "dateprocessed", "settlementdate", "trndate"},
    "amount": {"amount", "transactionamount", "amountaud", "value", "netamount",
               "amountinaccountcurrency"},
    "debit": {"debit", "debitamount", "withdrawal", "withdrawals", "moneyout",
              "paidout", "debitaud", "outflow"},
    "credit": {"credit", "creditamount", "deposit", "deposits", "moneyin", "paidin",
               "creditaud", "inflow"},
    "balance": {"balance", "runningbalance", "closingbalance", "balanceamount",
                "ledgerbalance", "availablebalance"},
    "description": {"description", "details", "narration", "memo", "particulars",
                    "transactiondetails", "transactiondescription", "reference",
                    "payee", "merchant", "name", "narrative", "note"},
    "indicator": {"type", "transactiontype", "drcr", "debitcredit", "cr_dr",
                  "creditdebitindicator", "direction"},
}
HEADER_SEARCH_ROWS = 12
SAMPLE_ROWS = 60
MATCH_THRESHOLD = 0.6


def _normalise_header(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").casefold())


def _header_field(text: str) -> str | None:
    key = _normalise_header(text)
    if not key:
        return None
    for field_name, words in HINTS.items():
        if key in words:
            return field_name
    # A loose second pass: a header containing the word still identifies the field.
    for field_name, words in HINTS.items():
        if any(word in key for word in words if len(word) >= 4):
            return field_name
    return None


def find_header_row(rows: Sequence[Sequence[str]]) -> int | None:
    """The index of the header row, or ``None`` when the export has none.

    Preamble junk above the header is the norm for a downloaded statement, so this scans
    the first dozen rows for one that names at least two fields rather than assuming
    line one.
    """
    for index, row in enumerate(rows[:HEADER_SEARCH_ROWS]):
        named = [field_name for field_name in (_header_field(cell) for cell in row)
                 if field_name is not None]
        if len(set(named)) >= 2:
            return index
    return None


def find_first_data_row(rows: Sequence[Sequence[str]], start: int) -> int | None:
    """The first row that looks like a transaction rather than more preamble."""
    for index in range(start, len(rows)):
        cells = [str(cell or "").strip() for cell in rows[index]]
        if not any(cells):
            continue
        has_date = any(parse_date(cell) is not None for cell in cells)
        has_amount = any(looks_like_amount(cell) for cell in cells)
        if has_date and has_amount:
            return index
    return None


def _column_scores(sample: Sequence[Sequence[str]]) -> list[dict[str, float]]:
    width = max((len(row) for row in sample), default=0)
    scores = [{"date": 0.0, "amount": 0.0, "text": 0.0, "length": 0.0, "filled": 0.0}
              for _ in range(width)]
    if not sample:
        return scores

    for column in range(width):
        cells = [str(row[column]).strip() for row in sample
                 if column < len(row) and str(row[column]).strip()]
        if not cells:
            continue
        total = len(cells)
        dates = sum(1 for cell in cells if parse_date(cell) is not None)
        amounts = sum(1 for cell in cells if looks_like_amount(cell))
        scores[column]["date"] = dates / total
        scores[column]["amount"] = amounts / total
        scores[column]["filled"] = total / len(sample)
        scores[column]["length"] = sum(len(cell) for cell in cells) / total
        # Text score: cells that are neither a date nor a number, which is what a
        # description column is made of.
        words = sum(1 for cell in cells
                    if parse_date(cell) is None and not looks_like_amount(cell)
                    and any(ch.isalpha() for ch in cell))
        scores[column]["text"] = words / total
    return scores


def detect_columns(rows: Sequence[Sequence[str]], headers: Sequence[str] = ()) -> ColumnChoice:
    """Which column is the date, the amount(s) and the description (§20).

    Headers win where they exist. Where they do not — or where they are unhelpful — each
    column is scored against date and amount predicates over a sample, already-chosen
    columns are excluded, and position is the last resort. Every choice is recorded with
    the reason it was made, so an import review can say *why* it read a file that way.
    """
    if not rows:
        return ColumnChoice(reason="no data rows")

    sample = rows[:SAMPLE_ROWS]
    named: dict[str, int] = {}
    for index, header in enumerate(headers):
        field_name = _header_field(header)
        if field_name and field_name not in named:
            named[field_name] = index

    scores = _column_scores(sample)
    width = len(scores)
    reasons: list[str] = []

    def take(field_name: str) -> int | None:
        index = named.get(field_name)
        if index is None or index >= width:
            return None
        reasons.append(f"{field_name} column from header {headers[index]!r}")
        return index

    date_index = take("date")
    if date_index is None:
        ranked = sorted(range(width), key=lambda c: -scores[c]["date"])
        if ranked and scores[ranked[0]]["date"] >= MATCH_THRESHOLD:
            date_index = ranked[0]
            reasons.append(f"date column {date_index} by content")

    debit_index = take("debit")
    credit_index = take("credit")
    balance_index = take("balance")
    indicator_index = take("indicator")
    amount_index = take("amount")

    chosen = {index for index in (date_index, debit_index, credit_index, balance_index,
                                  indicator_index, amount_index) if index is not None}

    if amount_index is None and debit_index is None and credit_index is None:
        ranked = [c for c in sorted(range(width), key=lambda c: -scores[c]["amount"])
                  if c not in chosen and scores[c]["amount"] >= MATCH_THRESHOLD]
        if len(ranked) > 1 and balance_index is None:
            # With two number columns and nothing naming a balance, the one furthest to
            # the right is the running balance — by position, not by score, since the
            # balance column is usually the tidiest of the two.
            balance_index = max(ranked)
            reasons.append(f"column {balance_index} treated as a running balance")
            ranked = [c for c in ranked if c != balance_index]
        if ranked:
            amount_index = ranked[0]
            reasons.append(f"amount column {amount_index} by content")

    description_index = take("description")
    if description_index is None:
        ranked = [c for c in sorted(range(width),
                                    key=lambda c: -(scores[c]["text"] * scores[c]["length"]))
                  if c not in {date_index, amount_index, debit_index, credit_index,
                               balance_index, indicator_index}
                  and scores[c]["text"] >= MATCH_THRESHOLD]
        if ranked:
            description_index = ranked[0]
            reasons.append(f"description column {description_index} by content")

    if date_index is None and amount_index is None and debit_index is None:
        # Nothing scored: fall back to position, which is what §20 asks for last.
        date_index, amount_index = 0, min(1, width - 1)
        reasons.append("fell back to position")

    date_samples = [row[date_index] for row in sample
                    if date_index is not None and date_index < len(row)]
    day_first = infer_date_order(date_samples)

    confidence = 0.0
    if date_index is not None:
        confidence += scores[date_index]["date"]
    if amount_index is not None:
        confidence += scores[amount_index]["amount"]
    confidence /= 2 if amount_index is not None else 1
    if named:
        confidence = max(confidence, 0.85)

    return ColumnChoice(
        date=date_index, amount=amount_index, description=description_index,
        debit=debit_index, credit=credit_index, balance=balance_index,
        indicator=indicator_index, day_first=day_first, confidence=confidence,
        reason="; ".join(reasons) or "no columns identified",
    )


# -------------------------------------------------------------------------- CSV


def sniff_delimiter(text: str) -> str:
    """The delimiter that actually splits this file into fields.

    Counting occurrences is not enough. A semicolon-delimited export whose descriptions
    contain commas — ``2026-09-01;COLES, NORTHLAND, VIC;-12,34`` — has more commas than
    semicolons, so counting picks the comma, and then *every* row fails to read: a valid
    statement imports as nothing at all. A delimiter is judged instead by what it
    produces: the candidate yielding the most consistent, multi-field rows wins.
    """
    best = ","
    best_score: tuple[int, int] | None = None
    for candidate in (",", ";", "\t", "|"):
        widths = [
            len(row) for row in csv.reader(io.StringIO(text, newline=""), delimiter=candidate)
            if any(cell.strip() for cell in row)
        ]
        fields = max(widths, default=0)
        if fields < 2:
            continue                       # not a delimiter here: it splits nothing
        counts: dict[int, int] = {}
        for width in widths:
            counts[width] = counts.get(width, 0) + 1
        # Consistency first — a real delimiter splits most rows the same way — then how
        # many fields it finds, so a correct 4-column read beats a plausible 2-column one.
        score = (max(counts.values()), fields)
        if best_score is None or score > best_score:
            best, best_score = candidate, score
    return best


def read_rows(text: str, delimiter: str | None = None) -> list[list[str]]:
    stream = io.StringIO(text, newline="")
    reader = csv.reader(stream, delimiter=delimiter or sniff_delimiter(text))
    return [row for row in reader]


def parse_csv(
    text: str,
    *,
    source: str = "csv",
    delimiter: str | None = None,
    column_map: ColumnChoice | None = None,
) -> ParseResult:
    """Parse a bank CSV, detecting its columns rather than assuming a schema."""
    rows = read_rows(text, delimiter)
    rows = [row for row in rows if any(str(cell).strip() for cell in row)]
    if not rows:
        return ParseResult(source=source, warnings=["the file has no rows"])

    header_row = find_header_row(rows)
    headers = [str(cell).strip() for cell in rows[header_row]] if header_row is not None else []
    data_start = (header_row + 1) if header_row is not None else 0
    if header_row is None:
        found = find_first_data_row(rows, 0)
        data_start = found if found is not None else 0

    data_rows = rows[data_start:]
    if not data_rows:
        return ParseResult(source=source, headers=headers, header_row=header_row,
                           warnings=["no transaction rows below the header"])

    columns = column_map or detect_columns(data_rows, headers)

    if column_map is None and header_row is None:
        # A preamble line can carry a bare date and a bare number — "Period Start,
        # 01/09/2026, Opening Balance, 1,234.56" — which passes the date-and-amount test
        # for where data begins, and then poisons column detection for the real rows
        # below it: its extra numeric cells win the amount slot, and every real row is
        # unreadable. Detection is only as good as the rows it samples, so the start is
        # chosen by which one actually parses rather than by the first row that looks
        # like data.
        best = _best_data_start(rows, data_start, headers)
        if best != data_start:
            data_start = best
            data_rows = rows[data_start:]
            columns = detect_columns(data_rows, headers)
    parsed: list[RawTxn] = []
    skipped: list[SkippedRow] = []

    for offset, row in enumerate(data_rows):
        line = data_start + offset + 1
        raw = {headers[i] if i < len(headers) else f"col{i}": str(cell)
               for i, cell in enumerate(row)}
        result = _parse_row(row, columns, raw)
        if isinstance(result, RawTxn):
            parsed.append(result)
        else:
            skipped.append(SkippedRow(line=line, reason=result, text=" | ".join(row)))

    warnings = []
    if header_row is None:
        warnings.append("no header row found; columns were detected from the data")
    if columns.confidence < MATCH_THRESHOLD:
        warnings.append(f"column detection is uncertain ({columns.reason})")
    return ParseResult(source=source, rows=parsed, skipped=skipped, columns=columns,
                       headers=headers, header_row=header_row, warnings=warnings)


def _parses_cleanly(rows: Sequence[Sequence[str]], columns: ColumnChoice) -> int:
    """How many rows read cleanly under this column mapping."""
    read = 0
    for row in rows:
        if not any(str(cell).strip() for cell in row):
            continue
        if isinstance(_parse_row(row, columns, {}), RawTxn):
            read += 1
    return read


def _best_data_start(rows: Sequence[Sequence[str]], start: int,
                     headers: Sequence[str] = (), limit: int = 20) -> int:
    """The start row that reads the *most* rows, earliest winning ties.

    Counting rows rather than taking a rate is what makes this safe. A rate would let a
    one-row suffix that happens to parse perfectly beat a longer prefix that reads more
    of the file — and a file with genuinely unreadable rows in the middle would then lose
    the good rows above them. Skipping a preamble line is only worth doing when it
    *costs* rows to keep it.
    """
    best = start
    best_read = -1
    ceiling = min(len(rows), start + limit)
    for candidate in range(start, ceiling):
        probe = rows[candidate:candidate + SAMPLE_ROWS]
        if not probe:
            break
        columns = detect_columns(probe, headers)
        if not columns.ok:
            continue
        read = _parses_cleanly(probe, columns)
        if read > best_read:
            best, best_read = candidate, read
        if best_read >= len(probe):
            break            # nothing left to gain
    return best


def _parse_row(row: Sequence[str], columns: ColumnChoice, raw: dict[str, str]) -> RawTxn | str:
    """A row as a :class:`RawTxn`, or the reason it could not be read."""
    def cell(index: int | None) -> str:
        if index is None or index >= len(row):
            return ""
        return str(row[index]).strip()

    when = parse_date(cell(columns.date), day_first=columns.day_first)
    if when is None:
        return f"no readable date in {cell(columns.date)!r}"

    if columns.debit is not None or columns.credit is not None:
        debit = parse_amount(cell(columns.debit)) or 0.0
        credit = parse_amount(cell(columns.credit)) or 0.0
        amount = abs(credit) - abs(debit)
    else:
        amount = parse_amount(cell(columns.amount))
        if amount is None:
            return f"no readable amount in {cell(columns.amount)!r}"
        signal = parse_indicator(cell(columns.indicator))
        if signal is not None:
            amount = abs(amount) * signal

    description = " ".join(cell(columns.description).split())
    if not description:
        description = " ".join(
            value for key, value in raw.items()
            if value and parse_date(value) is None and parse_amount(value) is None
        ).strip()

    external = None
    for key, value in raw.items():
        if value and _normalise_header(key) in {"fitid", "transactionid", "id", "referenceid"}:
            external = value
            break

    return RawTxn(date=when, amount=amount, description=description,
                  external_id=external, raw=raw)


# -------------------------------------------------------------------------- OFX

# OFX 1.x is SGML with unclosed tags and OFX 2.x is XML; a tag scanner reads both, where
# an XML parser silently finds nothing in half the files a bank produces.
_OFX_TAG = re.compile(r"<([A-Za-z0-9._/]+)>\s*([^<\r\n]*)", re.MULTILINE)


def parse_ofx(text: str, *, source: str = "ofx") -> ParseResult:
    """Parse an OFX/QFX statement into the same normalised shape as CSV."""
    if not text.strip():
        return ParseResult(source=source, warnings=["the file is empty"])

    records: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for name, value in _OFX_TAG.findall(text):
        tag = name.strip().upper()
        value = value.strip()
        if tag == "STMTTRN":
            current = {}
            continue
        if tag == "/STMTTRN":
            if current is not None:
                records.append(current)
            current = None
            continue
        if current is not None and value:
            current.setdefault(tag, value)

    if not records:
        return ParseResult(source=source, warnings=["no transactions found in the file"])

    parsed: list[RawTxn] = []
    skipped: list[SkippedRow] = []
    external_ids: set[str] = set()

    for index, record in enumerate(records):
        posted = record.get("DTPOSTED", "")
        amount = parse_amount(record.get("TRNAMT", ""))
        if amount is None:
            skipped.append(SkippedRow(line=index + 1, reason="no TRNAMT", text=str(record)))
            continue
        when = parse_ofx_date(posted)
        if when is None:
            skipped.append(SkippedRow(line=index + 1, reason=f"unreadable DTPOSTED {posted!r}",
                                      text=str(record)))
            continue

        description = " ".join(part for part in
                               (record.get("NAME", ""), record.get("MEMO", ""),
                                record.get("PAYEE", "")) if part).strip()
        if not description:
            description = " ".join(part for part in
                                   (record.get("TRNTYPE", ""), record.get("CHECKNUM", ""))
                                   if part).strip() or "Unknown"

        fitid = record.get("FITID") or None
        if fitid:
            if fitid in external_ids:
                skipped.append(SkippedRow(line=index + 1,
                                          reason=f"duplicate FITID {fitid}",
                                          text=str(record)))
                continue
            external_ids.add(fitid)

        parsed.append(RawTxn(date=when, amount=amount, description=description,
                             external_id=fitid, raw=dict(record)))

    return ParseResult(
        source=source, rows=parsed, skipped=skipped,
        columns=ColumnChoice(confidence=1.0, reason="OFX fields, not columns"),
        headers=sorted({key for record in records for key in record}),
        warnings=[],
    )


def parse_ofx_date(text: str) -> date | None:
    """``YYYYMMDDHHMMSS[.mmm][tz]`` — the first eight characters are the date."""
    digits = re.sub(r"[^0-9]", "", str(text or ""))
    if len(digits) < 8:
        return None
    try:
        return date(int(digits[0:4]), int(digits[4:6]), int(digits[6:8]))
    except ValueError:
        return None


# --------------------------------------------------------------------- dispatch

OFX_SUFFIXES = {".ofx", ".qfx"}

# CAMT.053 is not implemented: §20 marks it not required and §38 says it is worth an
# afternoon only if such a file ever actually arrives. The dispatch below is the single
# entry point it would plug into.


def decode(data: bytes) -> str:
    """Text from bytes, trying the encodings bank exports actually use."""
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def parse_file(path: str | Path, *, source: str | None = None) -> ParseResult:
    """Read and parse a statement file, dispatching on what it actually is."""
    target = Path(path)
    label = source or target.name
    try:
        text = decode(target.read_bytes())
    except OSError as exc:
        return ParseResult(source=label, warnings=[f"could not read {target.name}: {exc}"])

    suffix = target.suffix.casefold()
    if suffix in OFX_SUFFIXES or _looks_like_ofx(text):
        return parse_ofx(text, source=label)
    return parse_csv(text, source=label)


def _looks_like_ofx(text: str) -> bool:
    head = text[:2048].upper()
    return "OFXHEADER" in head or "<OFX>" in head or "<STMTTRN>" in head


def parse_bytes(data: bytes, *, source: str = "upload") -> ParseResult:
    """Parse in-memory bytes — what a sync response gives us, with no file involved."""
    text = decode(data)
    if _looks_like_ofx(text):
        return parse_ofx(text, source=source)
    return parse_csv(text, source=source)


# ---------------------------------------------------------------- identity (§21)


def normalise_description(text: str) -> str:
    """Lowercased, whitespace-collapsed: the form identity is computed over."""
    return " ".join(str(text or "").split()).casefold()


def fingerprint(
    when: date,
    amount: float,
    description: str,
    account: str,
    source: str,
    *,
    index: int = 0,
) -> str:
    """A stable identity for a row with no external id (§21).

    The ``index`` is the disambiguator, and it is the whole reason this design exists:
    keying on date + amount + description alone means two genuine $4.50 coffees on the
    same day collide and the second is silently dropped. Within one batch, rows that
    share those three fields are numbered in file order, so both coffees survive — and
    re-importing the same file reproduces the same numbering, so both are still
    recognised as already present.
    """
    parts = [
        when.isoformat(),
        f"{round(float(amount), 2):.2f}",
        normalise_description(description),
        str(account or ""),
        str(source or ""),
        str(index),
    ]
    digest = hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()
    return digest[:20]


@dataclass(frozen=True)
class PlannedRow:
    raw: RawTxn
    fingerprint: str | None = None
    duplicate: bool = False
    reason: str = ""
    external_id: str | None = None


@dataclass(frozen=True)
class ImportPlan:
    source: str
    account: str
    rows: list[PlannedRow] = field(default_factory=list)

    @property
    def new(self) -> list[PlannedRow]:
        return [row for row in self.rows if not row.duplicate]

    @property
    def duplicates(self) -> list[PlannedRow]:
        return [row for row in self.rows if row.duplicate]

    @property
    def imports_nothing(self) -> bool:
        return not self.new


def plan_import(
    existing: Sequence[Transaction],
    rows: Sequence[RawTxn],
    *,
    source: str,
    account: str,
) -> ImportPlan:
    """Decide which rows are new and which the store already has.

    Identity is external id first, always; the fingerprint is the fallback for rows that
    have none, and is only ever compared against rows from the same source.
    """
    known_external = {t.external_id for t in existing if t.external_id}
    known_fingerprints = {t.fingerprint for t in existing if t.fingerprint}

    seen: dict[tuple, int] = {}
    seen_external: set[str] = set()
    planned: list[PlannedRow] = []

    for raw in rows:
        key = (raw.date, round(raw.amount, 2), normalise_description(raw.description))
        index = seen.get(key, 0)
        seen[key] = index + 1

        if raw.external_id:
            # An id that appears twice in one batch is the same transaction twice — the
            # aggregator repeating itself, not two payments. In-batch as well as against
            # the store, which is what the OFX parser already did.
            duplicate = (raw.external_id in known_external
                         or raw.external_id in seen_external)
            seen_external.add(raw.external_id)
            planned.append(PlannedRow(
                raw=raw, external_id=raw.external_id, duplicate=duplicate,
                reason="external id already imported" if duplicate else "",
            ))
            continue

        mark = fingerprint(raw.date, raw.amount, raw.description, account, source, index=index)
        duplicate = mark in known_fingerprints
        planned.append(PlannedRow(
            raw=raw, fingerprint=mark, duplicate=duplicate,
            reason="same date, amount and description already imported" if duplicate else "",
        ))

    return ImportPlan(source=source, account=account, rows=planned)


# ------------------------------------------------------------------- the pipeline


def build_transaction(
    planned: PlannedRow,
    *,
    source: str,
    account: str,
    rules: Sequence[Rule] | None = None,
    now: datetime | None = None,
) -> Transaction:
    """A stored transaction from a planned row, categorised on the way in."""
    raw = planned.raw
    decision = cat.categorise(raw.description, list(rules or []))
    return Transaction(
        id=new_id("transactions"),
        date=raw.date,
        amount=raw.amount,
        description=raw.description,
        account=account,
        category=decision.category,
        tags=list(decision.tags),
        external_id=raw.external_id,
        fingerprint=planned.fingerprint,
        source=source,
        imported_at=now or datetime.now(),
        rule_id=decision.rule_id,
    )


def import_rows(
    store,
    rows: Sequence[RawTxn],
    *,
    source: str,
    account: str,
    rules: Sequence[Rule] | None = None,
    now: datetime | None = None,
    skipped: Sequence[SkippedRow] = (),
    warnings: Sequence[str] = (),
) -> ImportSummary:
    """Run planned rows through categorisation and into the store.

    Duplicates are counted and reported rather than dropped quietly: a skip count that
    looks wrong is a signal, not noise (§21).
    """
    chosen_rules = list(store.doc.rules if rules is None else rules)
    plan = plan_import(store.view().transactions, rows, source=source, account=account)

    imported = duplicates = categorised = 0
    for planned in plan.rows:
        if planned.duplicate:
            duplicates += 1
            continue
        txn = build_transaction(planned, source=source, account=account,
                                rules=chosen_rules, now=now)
        store.doc.transactions.append(txn)
        imported += 1
        if txn.category:
            categorised += 1

    if imported:
        store.version += 1

    arrived = [planned.raw.date for planned in plan.new if planned.raw.date]
    return ImportSummary(
        source=source,
        read=len(rows),
        skipped=len(skipped),
        imported=imported,
        duplicates=duplicates,
        categorised=categorised,
        uncategorised=imported - categorised,
        skipped_samples=[f"line {row.line}: {row.reason}" for row in list(skipped)[:5]],
        warnings=list(warnings),
        account=account,
        span=(min(arrived), max(arrived)) if arrived else None,
    )


def import_parse_result(
    store,
    parsed: ParseResult,
    *,
    account: str,
    rules: Sequence[Rule] | None = None,
    now: datetime | None = None,
) -> ImportSummary:
    return import_rows(store, parsed.rows, source=parsed.source, account=account,
                       rules=rules, now=now, skipped=parsed.skipped,
                       warnings=parsed.warnings)


def import_file(
    store,
    path: str | Path,
    *,
    account: str,
    rules: Sequence[Rule] | None = None,
    now: datetime | None = None,
) -> ImportSummary:
    """The whole file path: read, parse, normalise, identify, categorise, store."""
    parsed = parse_file(path)
    return import_parse_result(store, parsed, account=account, rules=rules, now=now)


# ------------------------------------------------------------------ reconciliation


@dataclass(frozen=True)
class Match:
    """A transaction that looks like a specific plan occurrence."""

    transaction_id: str
    item_id: str
    occurrence: date
    amount: float
    score: float

    @property
    def is_exact(self) -> bool:
        return self.score >= 0.9


@dataclass(frozen=True)
class Reconciliation:
    matches: list[Match] = field(default_factory=list)
    unmatched_transactions: list[str] = field(default_factory=list)
    unpaid_occurrences: list[tuple[str, date]] = field(default_factory=list)

    @property
    def matched_count(self) -> int:
        return len(self.matches)

    @property
    def unmatched_count(self) -> int:
        return len(self.unmatched_transactions)


AMOUNT_TOLERANCE = 0.02
DATE_WINDOW_DAYS = 10


def reconcile(
    view: StoreView,
    *,
    start: date,
    end: date,
) -> Reconciliation:
    """Match imported transactions against plan occurrences (§23).

    A match needs the same merchant *and* an amount inside a cent or two, on a date
    inside a ten-day window. Anything looser would start marking the wrong month's rent
    as paid, and a reconciliation tool that guesses is worse than one that declines.

    This **asks**, it does not act: it takes a `StoreView` and returns what it found.
    Applying the answer is :func:`apply_reconciliation`, which is a write and therefore
    owns a `Store`, bumps its version, and cannot be reached from a read-only facade.
    """
    from finance_tool.engine import recurrence as rec
    from finance_tool.engine.ledger import leaf_items

    candidates: list[tuple[str, rec.Occurrence, str]] = []
    for item in (i for i in leaf_items(view.items) if i.type == "expense"):
        for occ in rec.occurrences(item, start, end):
            if occ.paid:
                continue
            candidates.append((item.id, occ, det.merchant_key(item.name)))

    taken: set[tuple[str, date]] = set()
    matches: list[Match] = []
    unmatched: list[str] = []

    for txn in view.transactions:
        if txn.date is None or not (start <= txn.date <= end) or txn.amount >= 0:
            continue

        best: Match | None = None
        for item_id, occ, item_key in candidates:
            if (item_id, occ.date) in taken:
                continue
            if abs(abs(txn.amount) - occ.amount) > AMOUNT_TOLERANCE:
                continue
            if not _plausible_merchant(txn, item_key, occ):
                continue
            distance = abs((txn.date - occ.date).days)
            if distance > DATE_WINDOW_DAYS:
                continue
            score = 1.0 - (distance / (DATE_WINDOW_DAYS * 2))
            if best is None or score > best.score:
                best = Match(transaction_id=txn.id, item_id=item_id, occurrence=occ.date,
                             amount=occ.amount, score=score)

        if best is None:
            unmatched.append(txn.id)
            continue
        taken.add((best.item_id, best.occurrence))
        matches.append(best)

    return Reconciliation(
        matches=matches,
        unmatched_transactions=unmatched,
        unpaid_occurrences=[(item_id, occ.date) for item_id, occ, _ in candidates
                            if (item_id, occ.date) not in taken],
    )


def apply_reconciliation(store, result: Reconciliation) -> int:
    """Mark the matched occurrences paid. Returns how many changed.

    A write, so it takes the `Store` rather than the view — which is what makes it bump
    ``store.version``. Marking through a read-only facade would both break §15.2 and leave
    every version-keyed hook cache holding the old paid state.
    """
    marked = 0
    for match in result.matches:
        item = store.view().item(match.item_id)
        if item is None or match.occurrence in item.paid:
            continue
        item.paid.append(match.occurrence)
        marked += 1
    if marked:
        store.version += 1
    return marked


def _plausible_merchant(txn: Transaction, item_key: str, occ) -> bool:
    """Same merchant, or the same category — the user's own two ways of saying "this"."""
    txn_key = det.merchant_key(txn.description)
    if txn_key and item_key and (txn_key == item_key or txn_key in item_key
                                 or item_key in txn_key):
        return True
    return bool(txn.category) and txn.category == (occ.definition.category or "")


def rolled_up_import_summaries(summaries: Iterable[ImportSummary]) -> ImportSummary:
    """Many imports reported as one, for a sync that touches several accounts."""
    read = skipped = imported = duplicates = categorised = uncategorised = 0
    samples: list[str] = []
    warnings: list[str] = []
    for summary in summaries:
        read += summary.read
        skipped += summary.skipped
        imported += summary.imported
        duplicates += summary.duplicates
        categorised += summary.categorised
        uncategorised += summary.uncategorised
        samples.extend(summary.skipped_samples)
        warnings.extend(summary.warnings)
    return ImportSummary(
        source="sync", read=read, skipped=skipped, imported=imported,
        duplicates=duplicates, categorised=categorised, uncategorised=uncategorised,
        skipped_samples=samples[:5], warnings=warnings,
    )
