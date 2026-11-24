"""CAMT.053 and PDF statements (§19–§20), and the wording helpers they share with CSV."""

import base64
import sys
import types
from datetime import date

from finance_tool.engine import importers as imp
from finance_tool.engine import statements as st

from fixtures import CAMT053


# ------------------------------------------------------------------------ CAMT.053


def test_camt053_rows_carry_merchant_dates_and_balances():
    parsed = st.parse_camt053(CAMT053)

    assert parsed.format == "camt053"
    assert [row.amount for row in parsed.rows] == [-57.8, 2500.0]
    assert parsed.rows[0].merchant == "Coles 1234"
    assert parsed.rows[0].external_id == "REF1"
    assert parsed.rows[0].value_date == date(2026, 10, 2)
    assert parsed.meta["account_name"] == "Everyday"
    assert parsed.meta["account_number_masked"] == "****5678"
    assert parsed.meta["currency"] == "AUD"
    assert parsed.meta["opening_balance"] == 1000.0
    assert parsed.meta["closing_balance"] == 942.2
    assert parsed.meta["period_start"] == date(2026, 10, 1)
    assert parsed.meta["period_end"] == date(2026, 10, 3)


def test_camt053_accepts_a_base64_wrapped_file():
    encoded = base64.b64encode(CAMT053.encode("utf-8")).decode("ascii")

    parsed = st.parse_camt053(encoded)

    assert [row.amount for row in parsed.rows] == [-57.8, 2500.0]


def test_camt053_recognised_from_bytes_without_a_suffix():
    parsed = imp.parse_bytes(CAMT053.encode("utf-8"))

    assert parsed.format == "camt053"
    assert parsed.read == 2


def test_camt053_read_from_an_xml_file(tmp_path):
    path = tmp_path / "statement.xml"
    path.write_text(CAMT053, encoding="utf-8")

    parsed = imp.parse_file(path)

    assert parsed.format == "camt053"
    assert parsed.rows[0].external_id == "REF1"


def test_an_unreadable_amount_is_skipped_not_raised():
    broken = CAMT053.replace('<Amt Ccy="AUD">57.80</Amt>',
                             '<Amt Ccy="AUD">not-a-number</Amt>')

    parsed = st.parse_camt053(broken)

    assert len(parsed.rows) == 1
    assert parsed.skipped[0].reason == "unreadable amount"


def test_a_camt_amount_with_a_thousands_separator_still_reads():
    parsed = st.parse_camt053(
        CAMT053.replace('<Amt Ccy="AUD">57.80</Amt>',
                        '<Amt Ccy="AUD">1,234.56</Amt>'))

    assert parsed.rows[0].amount == -1234.56


def test_statements_for_the_same_account_merge_when_a_field_is_omitted():
    start = CAMT053.index("<Stmt>")
    end = CAMT053.index("</Stmt>") + len("</Stmt>")
    block = CAMT053[start:end]
    same_account = (block.replace("<Nm>Everyday</Nm>", "")
                    .replace("2026-10-01", "2026-11-01")
                    .replace("2026-10-02", "2026-11-02")
                    .replace("2026-10-03", "2026-11-03")
                    .replace("REF1", "REF8").replace("REF2", "REF9"))
    combined = CAMT053.replace(block, block + same_account)

    parsed = st.parse_camt053(combined)

    assert len(parsed.rows) == 4
    assert not any("different accounts" in warning for warning in parsed.warnings)


def test_an_anonymous_first_block_does_not_absorb_conflicting_accounts():
    start = CAMT053.index("<Stmt>")
    end = CAMT053.index("</Stmt>") + len("</Stmt>")
    block = CAMT053[start:end]
    anonymous = (block.replace("<Nm>Everyday</Nm>", "")
                 .replace("<Id><Othr><Id>12345678</Id></Othr></Id>", ""))
    first_account = (block.replace("REF1", "REF8").replace("REF2", "REF9")
                     .replace("2026-10", "2026-11"))
    other_account = (block.replace("12345678", "87654321")
                     .replace("REF1", "REF10").replace("REF2", "REF11")
                     .replace("2026-10", "2026-12"))
    combined = CAMT053.replace(block, anonymous + first_account + other_account)

    parsed = st.parse_camt053(combined)

    assert len(parsed.rows) == 4
    assert len(parsed.skipped) == 2
    assert any("different accounts" in warning for warning in parsed.warnings)


def test_a_balance_in_another_currency_is_not_applied():
    usd_closing = CAMT053.replace('<Amt Ccy="AUD">942.20</Amt>',
                                  '<Amt Ccy="USD">942.20</Amt>')

    parsed = st.parse_camt053(usd_closing)

    assert parsed.meta["currency"] == "AUD"
    assert "closing_balance" not in parsed.meta


def test_the_closing_balance_date_is_read_when_present():
    with_date = CAMT053.replace(
        '<Bal><Tp><CdOrPrtry><Cd>CLBD</Cd></CdOrPrtry></Tp>',
        '<Bal><Tp><CdOrPrtry><Cd>CLBD</Cd></CdOrPrtry></Tp>'
        '<Dt><Dt>2026-10-31</Dt></Dt>')

    parsed = st.parse_camt053(with_date)

    assert parsed.meta["closing_date"] == date(2026, 10, 31)


def test_a_statement_without_account_currency_uses_the_entry_currency():
    usd = CAMT053.replace("<Ccy>AUD</Ccy>", "").replace('Ccy="AUD"', 'Ccy="USD"')

    parsed = st.parse_camt053(usd)

    assert parsed.meta["currency"] == "USD"


def test_an_entry_in_another_currency_is_skipped_not_imported():
    mixed = CAMT053.replace('<Amt Ccy="AUD">2500.00</Amt>',
                            '<Amt Ccy="USD">2500.00</Amt>')

    parsed = st.parse_camt053(mixed)

    assert len(parsed.rows) == 1
    assert parsed.skipped[0].reason == "amount is in USD, statement is AUD"
    assert any("different currency" in warning for warning in parsed.warnings)


def test_several_statements_for_different_accounts_only_read_the_first():
    start = CAMT053.index("<Stmt>")
    end = CAMT053.index("</Stmt>") + len("</Stmt>")
    block = CAMT053[start:end]
    other = (block.replace("12345678", "87654321")
             .replace("REF1", "REF8").replace("REF2", "REF9"))
    combined = CAMT053.replace(block, block + other)

    parsed = st.parse_camt053(combined)

    assert len(parsed.rows) == 2
    assert parsed.meta["account_number_masked"] == "****5678"
    assert any("different accounts" in warning for warning in parsed.warnings)
    assert len(parsed.skipped) == 2


def test_ofx_saved_with_an_xml_suffix_is_still_ofx(tmp_path):
    ofx = ("OFXHEADER:100\n<OFX><BANKTRANLIST><STMTTRN><TRNTYPE>DEBIT</TRNTYPE>"
           "<DTPOSTED>20260901</DTPOSTED><TRNAMT>-57.80</TRNAMT><FITID>X1</FITID>"
           "<NAME>COLES</NAME></STMTTRN></BANKTRANLIST></OFX>")
    path = tmp_path / "statement.xml"
    path.write_text(ofx, encoding="utf-8")

    parsed = imp.parse_file(path)

    assert parsed.format == "ofx"
    assert parsed.rows[0].amount == -57.8


def test_a_bad_camt_file_is_a_warning_not_an_exception():
    parsed = st.parse_camt053("this is not xml at all")

    assert parsed.rows == []
    assert parsed.warnings
    assert "CAMT.053" in parsed.warnings[0] or "camt" in parsed.warnings[0].lower()


# ---------------------------------------------------------------------------- PDF


class _Page:
    def __init__(self, text, tables):
        self._text = text
        self._tables = tables

    def extract_text(self):
        return self._text

    def extract_tables(self):
        return self._tables


class _PDF:
    def __init__(self, pages):
        self.pages = pages

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _install_pdfplumber(monkeypatch, pages):
    module = types.ModuleType("pdfplumber")
    module.open = lambda stream: _PDF(pages)
    monkeypatch.setitem(sys.modules, "pdfplumber", module)


STATEMENT_TEXT = (
    "ANZ Plus\n"
    "Statement period: 1 Sep 2026 to 30 Sep 2026\n"
    "BSB: 012-345 Account number: 12345678\n"
)


def test_pdf_tables_become_rows(monkeypatch):
    tables = [[
        ["Date", "Description", "Balance", "Amount"],
        ["01/09/2026", "COLES NORTHLAND", "942.20", "-57.80"],
        ["03/09/2026", "ACME PTY LTD SALARY", "3,442.20", "2,500.00"],
    ]]
    _install_pdfplumber(monkeypatch, [_Page(STATEMENT_TEXT, tables)])

    parsed = st.parse_pdf(b"%PDF-1.4 pretend")

    assert parsed.format == "pdf"
    assert [row.amount for row in parsed.rows] == [-57.8, 2500.0]
    assert parsed.rows[0].merchant == "Coles Northland"
    assert parsed.meta["account_number_masked"] == "****5678"
    assert parsed.meta["bsb"] == "012345"
    assert parsed.meta["period_start"] == date(2026, 9, 1)
    assert any("1 page" in warning for warning in parsed.warnings)


def test_pdf_reads_debit_credit_balance_columns(monkeypatch):
    tables = [[
        ["Date", "Description", "Debit", "Credit", "Balance"],
        ["01/09/2026", "COLES", "57.80", "", "942.20"],
        ["03/09/2026", "SALARY", "", "2,500.00", "3,442.20"],
    ]]
    _install_pdfplumber(monkeypatch, [_Page(STATEMENT_TEXT, tables)])

    parsed = st.parse_pdf(b"%PDF-1.4 pretend")

    assert [row.amount for row in parsed.rows] == [-57.8, 2500.0]


def test_a_description_containing_debit_is_not_mistaken_for_a_header(monkeypatch):
    tables = [[
        ["01/09/2026", "DIRECT DEBIT DETAILS 998877", "-57.80", "942.20"],
        ["03/09/2026", "SALARY", "2,500.00", "3,442.20"],
    ]]
    _install_pdfplumber(monkeypatch, [_Page("", tables)])

    parsed = st.parse_pdf(b"%PDF-1.4 pretend")

    assert [row.amount for row in parsed.rows] == [-57.8, 2500.0]


def test_a_running_balance_table_is_not_a_transaction_table(monkeypatch):
    tables = [[
        ["Date", "Description", "Balance"],
        ["30/09/2026", "BALANCE BROUGHT FORWARD", "942.20"],
    ]]
    _install_pdfplumber(monkeypatch, [_Page("", tables)])

    parsed = st.parse_pdf(b"%PDF-1.4 pretend")

    assert parsed.rows == []


def test_a_parenthesised_amount_is_read_as_a_negative(monkeypatch):
    tables = [[["01/09/2026", "COLES", "(57.80)", "942.20"]]]
    _install_pdfplumber(monkeypatch, [_Page("", tables)])

    parsed = st.parse_pdf(b"%PDF-1.4 pretend")

    assert parsed.rows[0].amount == -57.8


def test_a_debit_card_column_does_not_hide_the_amount_column(monkeypatch):
    tables = [[
        ["Date", "Description", "Debit Card", "Amount"],
        ["01/09/2026", "COLES", "Visa", "-57.80"],
    ]]
    _install_pdfplumber(monkeypatch, [_Page("", tables)])

    parsed = st.parse_pdf(b"%PDF-1.4 pretend")

    assert parsed.rows[0].amount == -57.8
    assert parsed.rows[0].description == "COLES"


def test_a_particulars_balance_table_is_not_transactions(monkeypatch):
    tables = [[
        ["Date", "Particulars", "Balance"],
        ["30/09/2026", "CARRIED FORWARD", "942.20"],
    ]]
    _install_pdfplumber(monkeypatch, [_Page("", tables)])

    parsed = st.parse_pdf(b"%PDF-1.4 pretend")

    assert parsed.rows == []


def test_a_headerless_balance_line_is_not_a_payment(monkeypatch):
    tables = [[["30/09/2026", "BALANCE BROUGHT FORWARD", "", "942.20"]]]
    _install_pdfplumber(monkeypatch, [_Page("", tables)])

    parsed = st.parse_pdf(b"%PDF-1.4 pretend")

    assert parsed.rows == []


def test_a_headerless_table_reads_amount_before_balance(monkeypatch):
    tables = [[["01/09/2026", "COLES", "-57.80", "942.20"]]]
    _install_pdfplumber(monkeypatch, [_Page("", tables)])

    parsed = st.parse_pdf(b"%PDF-1.4 pretend")

    assert parsed.rows[0].amount == -57.8
    assert parsed.rows[0].raw["balance"] == "942.20"


def test_pdf_falls_back_to_text_lines_and_merges_wrapped_descriptions(monkeypatch):
    text = ("01/09/2026 COLES NORTHLAND -57.80 942.20\n"
            "NORTHLAND VIC\n")
    _install_pdfplumber(monkeypatch, [_Page(text, [])])

    parsed = st.parse_pdf(b"%PDF-1.4 pretend")

    assert len(parsed.rows) == 1
    assert parsed.rows[0].amount == -57.8
    assert "NORTHLAND VIC" in parsed.rows[0].description
    assert any("falling back to text lines" in warning for warning in parsed.warnings)


def test_pdf_without_the_extra_reports_how_to_install_it(monkeypatch):
    monkeypatch.setitem(sys.modules, "pdfplumber", None)

    parsed = st.parse_pdf(b"%PDF-1.4 pretend")

    assert parsed.rows == []
    assert any("ingest" in warning for warning in parsed.warnings)


def test_pdf_bytes_are_dispatched_on_the_file_magic(monkeypatch):
    _install_pdfplumber(monkeypatch, [_Page("", [])])

    parsed = imp.parse_bytes(b"%PDF-1.4 pretend")

    assert parsed.format == "pdf"


# -------------------------------------------------------------------------- wording


def test_merchant_guessing_strips_card_noise_and_references():
    assert imp.guess_merchant(
        "VISA PURCHASE COLES NORTHLAND 123456 AUS") == "Coles Northland"


def test_clean_description_drops_long_reference_numbers():
    assert imp.clean_description("COLES 1234   NORTHLAND 123456") == \
        "COLES 1234 NORTHLAND"


def test_csv_rows_get_a_merchant_and_a_format():
    parsed = imp.parse_csv(
        "Date,Amount,Description\n"
        "01/10/2026,-57.80,VISA PURCHASE COLES NORTHLAND 123456 AUS\n")

    assert parsed.format == "csv"
    assert parsed.rows[0].merchant == "Coles Northland"
