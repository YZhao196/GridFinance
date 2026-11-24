"""CAMT.053 and PDF statement parsers (§19–§20).

Both produce the same :class:`~finance_tool.engine.importers.ParseResult` as CSV and OFX,
so identity, dedupe and categorisation do not know which format a row came from.

The parsers are deliberately total: a file they cannot read returns a result with a
warning, never an exception. The caller's job is to show the warning; the engine's job is
not to make a corrupt statement look like an empty one.

PDF reading needs the optional ``ingest`` extra (pdfplumber). Its import is lazy so the
rest of the app runs — and the test suite passes — without it.
"""

from __future__ import annotations

import base64
import binascii
import io
import re
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import date
from math import isfinite

from finance_tool.engine.importers import (
    ColumnChoice,
    ParseResult,
    RawTxn,
    SkippedRow,
    clean_description,
    guess_merchant,
    parse_amount,
    parse_date,
)


# ------------------------------------------------------------------------ CAMT.053


def _load_xml(raw: str) -> ET.Element:
    """The document element, accepting XML text or a base64-wrapped CAMT file."""
    text = (raw or "").lstrip("﻿").strip()
    if "<Document" not in text and "<BkToCstmrStmt" not in text:
        try:
            decoded = base64.b64decode(text, validate=False).decode("utf-8", errors="ignore")
        except (binascii.Error, ValueError):
            decoded = ""
        if "<Document" in decoded or "<BkToCstmrStmt" in decoded:
            text = decoded

    if "<Document" not in text and "<BkToCstmrStmt" not in text:
        raise ValueError("not a CAMT.053 document: no <Document> element found")

    # Drop the XML declaration so ElementTree accepts odd encodings.
    start = text.find("<")
    if start != -1 and text[start:start + 5].lower() == "<?xml":
        end = text.find("?>", start)
        if end != -1:
            text = text[end + 2:]

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"could not read the CAMT.053 XML ({exc})") from exc
    for element in root.iter():
        if "}" in element.tag:
            element.tag = element.tag.split("}", 1)[1]
    return root


def _child(element: ET.Element, *names: str) -> ET.Element | None:
    for name in names:
        found = element.find(name)
        if found is not None:
            return found
    return None


def _text(element: ET.Element, *path: str) -> str | None:
    node: ET.Element | None = element
    for step in path:
        if node is None:
            return None
        node = _child(node, step)
    if node is not None and node.text:
        return node.text.strip()
    return None


def _amount(element: ET.Element) -> tuple[float | None, str]:
    """A decimal amount as dollars, or ``None`` when it is not a finite number.

    The currency is returned with it; an omitted currency is unknown, not AUD. Assuming
    AUD is how a USD statement lands in an AUD account.
    """
    node = _child(element, "Amt")
    if node is None or not node.text:
        return None, ""
    value = parse_amount(node.text)
    if value is None or not isfinite(value):
        return None, (node.attrib.get("Ccy") or "").upper()
    return round(value, 2), (node.attrib.get("Ccy") or "").upper()


def _date_from(element: ET.Element, *path: str) -> date | None:
    raw = _text(element, *path)
    if not raw:
        return None
    if "T" in raw:
        raw = raw.split("T", 1)[0]
    return parse_date(raw)


def _balance_type(bal: ET.Element) -> str | None:
    return _text(bal, "Tp", "CdOrPrtry", "Cd")


def _identity(statement: ET.Element) -> tuple[str | None, str | None, str | None]:
    """Which account a statement block belongs to, for deciding whether blocks may merge."""
    return (
        _text(statement, "Acct", "Nm"),
        _text(statement, "Acct", "Id", "IBAN") or _text(statement, "Acct", "Id", "Othr", "Id"),
        (_text(statement, "Acct", "Ccy") or "").upper() or None,
    )


def _conflicts(known: tuple, other: tuple) -> bool:
    """Whether a block disagrees with what the chosen blocks already established.

    Optional fields are unknown, not disagreement: a block that omits ``Acct/Nm`` for the
    same IBAN is the same account. Tracking the facets cumulatively is what makes this
    transitive — two blocks whose IBANs differ cannot both join an anonymous first block.
    """
    known_name, known_id, known_currency = known
    name, identifier, currency = other
    if known_id and identifier and known_id != identifier:
        return True
    if known_name and name and known_name.casefold() != name.casefold():
        return True
    if known_currency and currency and known_currency != currency:
        return True
    return False


def _absorb(known: tuple, other: tuple) -> tuple:
    return tuple(known[i] or other[i] for i in range(3))


def parse_camt053(text: str, *, source: str = "camt053") -> ParseResult:
    """An ISO 20022 CAMT.053 statement as normalised rows."""
    try:
        root = _load_xml(text)
    except ValueError as exc:
        return ParseResult(source=source, format="camt053", warnings=[str(exc)])

    statements = root.findall(".//Stmt")
    if not statements:
        return ParseResult(source=source, format="camt053",
                           warnings=["CAMT.053 has no <Stmt> block"])

    warnings: list[str] = []
    base = _identity(statements[0])
    chosen = [statements[0]]
    omitted: list[ET.Element] = []
    for statement in statements[1:]:
        identity = _identity(statement)
        if _conflicts(base, identity):
            omitted.append(statement)
        else:
            chosen.append(statement)
            base = _absorb(base, identity)
    if omitted:
        # Blocks for different accounts cannot be merged: the rows would all be placed
        # into one account because the result carries one identity. Reading the first and
        # saying so is recoverable; misplacing a second account's money is not.
        label = base[0] or base[1] or "first account"
        warnings.append(
            f"the file contains statements for different accounts; only {label} was read "
            "— import each account's statement separately")

    rows: list[RawTxn] = []
    skipped: list[SkippedRow] = []
    balances: dict[str, float] = {}
    masked: str | None = None
    account_name: str | None = None
    currency = ""
    closing_date: date | None = None
    foreign_skips = 0

    for statement in chosen:
        account_name = account_name or _text(statement, "Acct", "Nm")
        iban = _text(statement, "Acct", "Id", "IBAN")
        other = _text(statement, "Acct", "Id", "Othr", "Id")
        account_id = iban or other
        if account_id:
            digits = "".join(ch for ch in account_id if ch.isdigit())
            if len(digits) >= 4:
                masked = f"****{digits[-4:]}"
        ccy = _text(statement, "Acct", "Ccy")
        if ccy and not currency:
            currency = ccy.upper()

        for bal in statement.findall("Bal"):
            code = _balance_type(bal)
            amount_node = _child(bal, "Amt")
            if not code or amount_node is None or not amount_node.text:
                continue
            balance_currency = (amount_node.attrib.get("Ccy") or "").upper()
            if balance_currency and not currency:
                currency = balance_currency
            if balance_currency and currency and balance_currency != currency:
                continue
            value = parse_amount(amount_node.text)
            if value is None or not isfinite(value):
                continue
            value = round(value, 2)
            if _text(bal, "CdtDbtInd") == "DBIT":
                value = -abs(value)
            balances[code] = value
            if code == "CLBD":
                closing_date = (_date_from(bal, "Dt", "Dt")
                                or _date_from(bal, "Dt", "DtTm")
                                or _date_from(bal, "Dt"))

        for entry in statement.findall("Ntry"):
            amount, entry_currency = _amount(entry)
            if amount is not None and _text(entry, "CdtDbtInd") == "DBIT":
                amount = -abs(amount)
            if entry_currency and not currency:
                currency = entry_currency

            booked = _date_from(entry, "BookgDt", "Dt") or _date_from(entry, "BookgDt", "DtTm")
            valued = _date_from(entry, "ValDt", "Dt") or _date_from(entry, "ValDt", "DtTm")

            parts: list[str] = []
            for detail in entry.findall("NtryDtls"):
                info = _text(detail, "TxDtls", "RmtInf", "Ustrd")
                if info:
                    parts.append(info)
                for party in ("Dbtr", "Cdtr"):
                    name = _text(detail, "TxDtls", "RltdPties", party, "Nm")
                    if name:
                        parts.append(name)
            extra = _text(entry, "AddtlNtryInf")
            if extra:
                parts.append(extra)
            description = clean_description(" · ".join(dict.fromkeys(parts)))
            description = description or "Unknown transaction"

            line = len(rows) + len(skipped) + 1
            if entry_currency and currency and entry_currency != currency:
                skipped.append(SkippedRow(
                    line=line,
                    reason=f"amount is in {entry_currency}, statement is {currency}",
                    text=description))
                foreign_skips += 1
                continue
            if amount is None:
                skipped.append(SkippedRow(line=line, reason="unreadable amount",
                                          text=description))
                continue
            if amount == 0:
                skipped.append(SkippedRow(line=line, reason="zero amount", text=description))
                continue
            if booked is None:
                skipped.append(SkippedRow(line=line, reason="no booked date",
                                          text=description))
                continue

            rows.append(RawTxn(
                date=booked,
                amount=amount,
                description=description,
                # NtryRef is a per-statement sequence, not a global identity; using it
                # would make next month's "1" a duplicate of this month's.
                external_id=_text(entry, "AcctSvcrRef"),
                merchant=guess_merchant(description),
                value_date=valued,
                raw={"currency": currency},
            ))

    if foreign_skips:
        warnings.append(
            f"{foreign_skips} entries were in a different currency and were skipped")

    for statement in omitted:
        for entry in statement.findall("Ntry"):
            skipped.append(SkippedRow(
                line=len(rows) + len(skipped) + 1,
                reason="belongs to a different account statement",
                text=_text(entry, "AddtlNtryInf") or "",
            ))

    if not rows:
        warnings.append("CAMT.053 contained no bookable entries")

    rows.sort(key=lambda row: row.date)
    meta: dict[str, object] = {}
    if currency:
        meta["currency"] = currency
    if masked:
        meta["account_number_masked"] = masked
    if account_name:
        meta["account_name"] = account_name
    if "OPBD" in balances:
        meta["opening_balance"] = balances["OPBD"]
    if "CLBD" in balances:
        meta["closing_balance"] = balances["CLBD"]
    if closing_date is not None:
        meta["closing_date"] = closing_date
    if rows:
        meta["period_start"] = rows[0].date
        meta["period_end"] = rows[-1].date

    return ParseResult(
        source=source, rows=rows, skipped=skipped,
        columns=ColumnChoice(confidence=1.0, reason="CAMT.053 fields"),
        warnings=warnings, meta=meta, format="camt053",
    )


# ---------------------------------------------------------------------------- PDF

AMOUNT = re.compile(
    r"^\(?[+-]?\$?\d{1,3}(?:,\d{3})*(?:\.\d{2})?\)?-?$|"
    r"^\(?[+-]?\$?\d+\.\d{2}\)?-?$"
)
DATE_TOKEN = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}[/\-\s](?:\d{1,2}|[A-Za-z]{3,9})[/\-\s]\d{2,4})\b"
)
LINE_AMOUNT = re.compile(r"\(?[+-]?\$?\d{1,3}(?:,\d{3})*\.\d{2}\)?-?")
PERIOD = re.compile(
    r"(?:statement\s+period|period)\s*:?\s*(.*?)\s*(?:to|-|–|until)\s*(.*?)(?:\n|$)",
    re.IGNORECASE,
)
BSB = re.compile(r"BSB[:\s]*([\d\-\s]{5,10})", re.IGNORECASE)
ACCOUNT = re.compile(r"account\s*(?:number|no\.?|#)?[:\s]*([\d\s\-]{4,20})", re.IGNORECASE)

# Header vocabulary for table layouts. A header row is worth reading: it is the only
# thing that distinguishes "Date, Description, Debit, Balance" from "Date, Description,
# Balance, Amount" without guessing which numeric column is the money.
HEADER_WORDS: dict[str, str] = {
    "date": r"(transaction date|posting date|posted date|value date|date)",
    "description": r"(description|details|particulars|narrative|memo|payee|reference)",
    "amount": r"(amount|transaction amount)",
    "debit": r"(debit|withdrawals?|money out)",
    "credit": r"(credit|deposits?|money in)",
    "balance": r"(balance|running balance|closing balance|opening balance)",
}


def _is_amount(cell: str | None) -> bool:
    text = (cell or "").strip().replace(" ", "")
    return bool(text) and bool(AMOUNT.match(text))


def _amount_from(cell: str | None) -> float:
    text = (cell or "").strip()
    trailing_minus = text.endswith("-")
    value = parse_amount(text.rstrip("-") if trailing_minus else text)
    if value is None or not isfinite(value):
        return 0.0
    return -abs(value) if trailing_minus else value


def _header_columns(rows: list[list[str]]) -> dict[str, int]:
    for row in rows[:6]:
        cells = [re.sub(r"\s+", " ", str(cell or "").strip()).casefold() for cell in row]
        # A data row carries a date and an amount; a header carries neither. Without this
        # guard a description like "DIRECT DEBIT DETAILS 998877" reads as a debit header.
        if any(parse_date(cell) for cell in cells if cell):
            continue
        if any(_is_amount(cell) for cell in cells if cell):
            continue
        labels = [re.sub(r"\(.*?\)", "", cell).strip(" :.-") for cell in cells]
        found: dict[str, int] = {}
        for key, pattern in HEADER_WORDS.items():
            for index, label in enumerate(labels):
                if re.fullmatch(pattern, label):
                    found.setdefault(key, index)
        if len(found) < 2:
            continue
        if len(set(found.values())) < len(found):
            # One cell cannot be both the date and the amount; a preamble that reads
            # that way is not a header row.
            continue
        has_money = any(key in found for key in ("amount", "debit", "credit"))
        if "date" in found and (has_money or "description" in found):
            return found
        if has_money and "description" in found:
            return found
    return {}


def _rows_to_transactions(rows: list[list[str]]) -> list[RawTxn]:
    out: list[RawTxn] = []
    header = _header_columns(rows)
    header_has_money = any(key in header for key in ("amount", "debit", "credit"))
    for row in rows:
        cells = [(cell or "").strip() for cell in row]
        if not any(cells):
            continue
        if header and not header_has_money:
            # Date and description but no amount column: a running-balance table is not
            # a transaction table, and reading its balance as an amount invents money.
            continue
        numeric_all = [i for i, cell in enumerate(cells) if _is_amount(cell)]

        date_index = header.get("date")
        posted = (parse_date(cells[date_index])
                  if date_index is not None and date_index < len(cells) else None)
        if posted is None:
            date_index = next((i for i, cell in enumerate(cells) if parse_date(cell)), None)
            if date_index is None:
                continue
            posted = parse_date(cells[date_index])
        if posted is None:
            continue

        excluded = {date_index}
        balance_index = header.get("balance")
        amount: float | None = None

        if "amount" in header and header["amount"] < len(cells):
            # An explicit Amount column wins over a "Debit"/"Credit" label: "Debit Card"
            # is a card column, not a money column.
            amount_index = header["amount"]
            amount = _amount_from(cells[amount_index])
            excluded.add(amount_index)
        elif "debit" in header or "credit" in header:
            debit_index = header.get("debit")
            credit_index = header.get("credit")
            debit = (abs(_amount_from(cells[debit_index]))
                     if debit_index is not None and debit_index < len(cells) else 0.0)
            credit = (abs(_amount_from(cells[credit_index]))
                      if credit_index is not None and credit_index < len(cells) else 0.0)
            amount = credit - debit
            excluded |= {index for index in (debit_index, credit_index) if index is not None}
        else:
            if not numeric_all:
                continue
            # Headerless and two numerics: the first is the amount and the last the
            # running balance on the layouts these statements use. Headers settle it
            # whenever they exist; without one, that is the least-wrong reading.
            amount_index = (numeric_all[0] if len(numeric_all) > 1 else numeric_all[-1])
            amount = _amount_from(cells[amount_index])
            excluded.add(amount_index)
            if balance_index is None and len(numeric_all) > 1:
                balance_index = numeric_all[-1]

        if balance_index is not None:
            excluded.add(balance_index)

        description_index = header.get("description")
        if description_index is not None and description_index < len(cells):
            description = cells[description_index]
            excluded.add(description_index)
        else:
            description = " ".join(
                cells[i] for i in range(len(cells)) if i not in excluded and cells[i])
        description = clean_description(description)

        if (not header and len(numeric_all) == 1
                and re.search(r"\b(balance|total)\b", description, re.IGNORECASE)):
            # A dated carry-forward line with just the running balance is not a payment.
            continue
        if amount is None or amount == 0 or not description:
            continue
        out.append(RawTxn(
            date=posted, amount=amount, description=description,
            merchant=guess_merchant(description),
            raw={"cells": cells,
                 "balance": cells[balance_index]
                 if balance_index is not None and balance_index < len(cells) else ""},
        ))
    return out


def _lines_to_transactions(page_text: str) -> list[RawTxn]:
    out: list[RawTxn] = []
    pending = ""
    for raw_line in page_text.splitlines():
        line = " ".join(raw_line.split())
        if not line:
            continue

        date_match = DATE_TOKEN.search(line)
        tail_amounts = LINE_AMOUNT.findall(line)
        if not date_match or not tail_amounts:
            # A wrapped description continues the previous row.
            if out and len(line) > 3 and not re.search(r"(?:balance|total|page)\b", line,
                                                       re.IGNORECASE):
                pending = f"{pending} {line}".strip()
                if pending:
                    merged = clean_description(f"{out[-1].description} {pending}")
                    out[-1] = replace(out[-1], description=merged,
                                      merchant=guess_merchant(merged))
                pending = ""
            continue

        posted = parse_date(date_match.group(1))
        if posted is None:
            continue
        start, end = date_match.span()
        tail = line[end:]
        amounts = LINE_AMOUNT.findall(tail)
        if not amounts:
            continue
        amount_raw = amounts[0]
        description = clean_description(tail[:tail.find(amount_raw)])
        if not description:
            continue
        amount = _amount_from(amount_raw)
        if amount == 0:
            continue
        out.append(RawTxn(
            date=posted, amount=amount, description=description,
            merchant=guess_merchant(description),
            raw={"line": line, "prefix": line[:start]},
        ))
        pending = ""
    return out


def parse_pdf(data: bytes, *, source: str = "pdf") -> ParseResult:
    """A PDF statement as normalised rows, tables first and text lines as the fallback."""
    try:
        import pdfplumber
    except ImportError:
        return ParseResult(
            source=source, format="pdf",
            warnings=["PDF statements need the 'ingest' extra: "
                      "pip install gridfinance[ingest]"],
        )

    rows: list[RawTxn] = []
    chunks: list[str] = []
    warnings: list[str] = []
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            warnings.append(f"read {len(pdf.pages)} page(s)")
            for page in pdf.pages:
                chunks.append(page.extract_text() or "")
                for table in page.extract_tables() or []:
                    table_rows = [[(cell or "") for cell in row] for row in table]
                    rows.extend(_rows_to_transactions(table_rows))
    except Exception as exc:  # pdfminer raises several unrelated exception types
        return ParseResult(source=source, format="pdf",
                           warnings=[f"could not read the PDF ({exc})"])

    full_text = "\n".join(chunks)
    if not rows:
        warnings.append("no table rows recognised; falling back to text lines")
        rows = _lines_to_transactions(full_text)

    period = PERIOD.search(full_text)
    period_start = parse_date(period.group(1).strip()) if period else None
    period_end = parse_date(period.group(2).strip()) if period else None
    bsb_match = BSB.search(full_text)
    bsb = re.sub(r"\D", "", bsb_match.group(1))[:6] if bsb_match else ""
    account_match = ACCOUNT.search(full_text)
    masked = None
    if account_match:
        digits = re.sub(r"\D", "", account_match.group(1))
        if len(digits) >= 4:
            masked = f"****{digits[-4:]}"

    if not rows:
        warnings.append("could not read any transactions; a scanned image has no text "
                        "layer — export CSV instead")
        return ParseResult(source=source, format="pdf", warnings=warnings)

    rows.sort(key=lambda row: row.date)
    meta: dict[str, object] = {
        "period_start": period_start or rows[0].date,
        "period_end": period_end or rows[-1].date,
    }
    if bsb:
        meta["bsb"] = bsb
    if masked:
        meta["account_number_masked"] = masked

    return ParseResult(
        source=source, rows=rows,
        columns=ColumnChoice(confidence=1.0, reason="PDF tables/text"),
        warnings=warnings, meta=meta, format="pdf",
    )
