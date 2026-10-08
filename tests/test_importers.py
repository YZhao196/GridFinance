from datetime import date, datetime
from pathlib import Path

import pytest

from finance_tool.engine import importers as imp
from finance_tool.store.entities import (
    Item,
    Rule,
    StoreDocument,
    Transaction,
    EXPENSE,
)
from finance_tool.store.store import Store

STATEMENTS = Path(__file__).resolve().parent / "statements"


def statement(name: str) -> Path:
    return STATEMENTS / name


# ------------------------------------------------------------------- date parsing


@pytest.mark.parametrize("text,expected", [
    ("2026-09-01", date(2026, 9, 1)),
    ("2026/09/01", date(2026, 9, 1)),
    ("20260901", date(2026, 9, 1)),
    ("01/09/2026", date(2026, 9, 1)),
    ("1/9/2026", date(2026, 9, 1)),
    ("07 Oct 2026", date(2026, 10, 7)),
    ("7 October 2026", date(2026, 10, 7)),
    ("Oct 7, 2026", date(2026, 10, 7)),
    ("01-09-2026", date(2026, 9, 1)),
    ("01.09.2026", date(2026, 9, 1)),
    ('"01/09/2026"', date(2026, 9, 1)),
])
def test_parse_date_reads_what_banks_print(text, expected):
    assert imp.parse_date(text) == expected


def test_a_two_digit_year_is_this_century():
    assert imp.parse_date("01/09/26") == date(2026, 9, 1)
    assert imp.parse_date("01/09/99") == date(1999, 9, 1)


def test_the_ambiguous_order_is_caller_controlled():
    assert imp.parse_date("01/09/2026", day_first=True) == date(2026, 9, 1)
    assert imp.parse_date("01/09/2026", day_first=False) == date(2026, 1, 9)


def test_a_day_above_twelve_settles_the_order_by_itself():
    assert imp.parse_date("13/01/2026", day_first=True) == date(2026, 1, 13)
    assert imp.parse_date("01/13/2026", day_first=True) == date(2026, 1, 13)


@pytest.mark.parametrize("text", ["", None, "not a date", "31/02/2026", "2026-13-01", "0/0/0"])
def test_unreadable_dates_are_none_rather_than_a_guess(text):
    assert imp.parse_date(text) is None


def test_infer_date_order_reads_the_whole_column():
    assert imp.infer_date_order(["01/09/2026", "13/09/2026"]) is True
    assert imp.infer_date_order(["01/09/2026", "01/13/2026"]) is False
    # Nothing to go on: the app's own locale decides rather than guessing per row.
    assert imp.infer_date_order(["01/09/2026"]) is True
    assert imp.infer_date_order(["01/09/2026"], default_day_first=False) is False
    assert imp.infer_date_order(["2026-09-01"]) is True


# ----------------------------------------------------------------- amount parsing


@pytest.mark.parametrize("text,expected", [
    ("-520.00", -520.00),
    ("520.00", 520.00),
    ("$1,234.56", 1234.56),
    ("-$1,234.56", -1234.56),
    ("(1,234.56)", -1234.56),
    ("1234.56-", -1234.56),
    ("+50.00", 50.00),
    ("1,234", 1234.00),
    ("12,34", 12.34),
    ("3200", 3200.00),
    ("1.234,56", 1234.56),  # European separators, read the other way round
    ("1-2-3", None),        # genuinely unreadable, so it declines
])
def test_parse_amount_handles_the_conventions_banks_use(text, expected):
    assert imp.parse_amount(text) == expected


@pytest.mark.parametrize("text", ["", None, "abc", "-", "$", "RENT"])
def test_unreadable_amounts_are_none(text):
    assert imp.parse_amount(text) is None


def test_parse_amount_is_permissive_which_is_why_column_choice_is_strict():
    """It lifts the number out of a cell it has been told is the amount column.

    "$1,234.56 AUD" and "1,200 CR" are both things a bank writes in an amount cell, so
    this cannot reject a cell for containing letters. Column *choice* is where that
    strictness lives, in :func:`looks_like_amount`.
    """
    assert imp.parse_amount("$1,234.56 AUD") == pytest.approx(1234.56)
    assert imp.parse_amount("CAFE LATTE 0091") == pytest.approx(91.0)
    assert imp.looks_like_amount("CAFE LATTE 0091") is False


def test_looks_like_amount_is_stricter_than_parse_amount():
    """Choosing columns needs "is this money", not "is there a number in here"."""
    assert imp.looks_like_amount("-520.00") is True
    assert imp.looks_like_amount("$1,234.56") is True
    # Both parse as numbers; neither is an amount.
    assert imp.looks_like_amount("CAFE LATTE 0091") is False
    assert imp.looks_like_amount("2026-09-01") is False


def test_parse_indicator():
    assert imp.parse_indicator("DR") == -1
    assert imp.parse_indicator("debit") == -1
    assert imp.parse_indicator("CR") == 1
    assert imp.parse_indicator("Deposit") == 1
    assert imp.parse_indicator("") is None
    assert imp.parse_indicator("EFTPOS") is None


# ------------------------------------------------------------------ CSV fixtures


def test_a_statement_with_preamble_junk_finds_its_header():
    result = imp.parse_file(statement("statement_preamble.csv"))

    assert result.header_row == 3
    assert result.headers == ["Date", "Description", "Debit", "Credit", "Balance"]
    assert result.columns.date == 0
    assert result.columns.description == 1
    assert result.columns.debit == 2
    assert result.columns.credit == 3
    assert result.columns.balance == 4
    assert result.columns.amount is None
    assert result.columns.confidence == pytest.approx(1.0)
    assert "header" in result.columns.reason
    assert result.warnings == []


def test_the_preamble_statement_reads_every_row():
    result = imp.parse_file(statement("statement_preamble.csv"))

    assert result.read == 15
    assert result.skipped == []
    assert result.describe() == "15 read"
    assert result.rows[0].date == date(2026, 9, 1)
    assert result.rows[0].amount == pytest.approx(-520.00)
    assert result.rows[0].description == "RENT PAYMENT 8821"
    assert result.rows[1].amount == pytest.approx(3200.00)
    assert result.rows[0].external_id is None


def test_a_debit_credit_pair_becomes_one_signed_amount():
    result = imp.parse_file(statement("statement_preamble.csv"))
    by_description = {row.description: row.amount for row in result.rows}
    assert by_description["RENT PAYMENT 8821"] == pytest.approx(-520.00)
    assert by_description["SALARY ACME PTY"] == pytest.approx(3200.00)

    expenses = sum(abs(r.amount) for r in result.rows if r.amount < 0)
    income = sum(r.amount for r in result.rows if r.amount > 0)
    assert expenses == pytest.approx(1172.59)
    assert income == pytest.approx(3200.00)
    assert sum(r.amount for r in result.rows) == pytest.approx(3200.00 - 1172.59)


def test_two_genuine_same_day_same_amount_transactions_both_survive():
    """The bug §21 exists to avoid: the second $4.50 coffee must not vanish."""
    result = imp.parse_file(statement("statement_preamble.csv"))
    coffees = [r for r in result.rows if r.description == "CAFE LATTE 0091"
               and r.date == date(2026, 9, 22)]
    assert len(coffees) == 2
    assert all(c.amount == pytest.approx(-4.50) for c in coffees)


def test_a_signed_amount_column_is_read_directly():
    result = imp.parse_file(statement("statement_signed.csv"))

    assert result.header_row == 0
    assert result.columns.amount == 2
    assert result.columns.balance == 3
    assert result.read == 3
    assert [r.amount for r in result.rows] == [-520.00, -4.50, -79.00]


def test_a_headerless_export_still_detects_its_columns():
    result = imp.parse_file(statement("statement_headerless.csv"))

    assert result.header_row is None
    assert result.headers == []
    assert result.columns.date == 0
    assert result.columns.amount == 2
    assert result.columns.description == 1
    # The trailing number column is recognised as a running balance, not the amount.
    assert result.columns.balance == 3
    assert any("no header" in warning for warning in result.warnings)
    assert [r.amount for r in result.rows] == [-520.00, -4.50, -79.00]


def test_unreadable_rows_are_counted_and_explained():
    result = imp.parse_file(statement("statement_junk.csv"))

    assert result.read == 2
    assert len(result.skipped) == 2
    assert result.describe() == "2 read, 2 skipped"
    assert [s.line for s in result.skipped] == [2, 3]
    assert "amount" in result.skipped[0].reason
    assert "date" in result.skipped[1].reason
    assert result.skipped[0].text


def test_a_missing_file_is_reported_not_raised():
    result = imp.parse_file(statement("does-not-exist.csv"))
    assert result.rows == []
    assert any("could not read" in warning for warning in result.warnings)


def test_an_empty_file_is_reported_not_raised(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")
    result = imp.parse_file(path)
    assert result.rows == []
    assert result.warnings


def test_a_preamble_only_file_is_reported_not_raised(tmp_path):
    path = tmp_path / "preamble.csv"
    path.write_text("Account Name,Foo\nStatement Period,nowhere\n", encoding="utf-8")
    result = imp.parse_file(path)
    assert result.rows == []
    assert result.warnings


# -------------------------------------------------------------------------- OFX


def test_ofx_sgml_with_unclosed_tags_is_read():
    result = imp.parse_file(statement("statement.ofx"))

    assert result.read == 5
    assert len(result.skipped) == 1
    assert result.describe() == "5 read, 1 skipped"
    assert "duplicate FITID" in result.skipped[0].reason

    first = result.rows[0]
    assert first.date == date(2026, 9, 1)
    assert first.amount == pytest.approx(-520.00)
    assert first.description == "RENT PAYMENT 8821"
    assert first.external_id == "2026090100001"


def test_ofx_keeps_the_aggregators_transaction_id():
    result = imp.parse_file(statement("statement.ofx"))
    assert [r.external_id for r in result.rows] == [
        "2026090100001", "2026090400001", "2026090500001",
        "2026092200001", "2026092200002",
    ]


def test_ofx_amounts_keep_their_own_sign():
    result = imp.parse_file(statement("statement.ofx"))
    assert result.rows[0].amount == pytest.approx(-520.00)
    assert result.rows[1].amount == pytest.approx(3200.00)


def test_ofx_dates_survive_timezone_and_time_suffixes():
    result = imp.parse_file(statement("statement.ofx"))
    assert imp.parse_ofx_date("20260922000000[-5:EST]") == date(2026, 9, 22)
    assert imp.parse_ofx_date("20260904000000") == date(2026, 9, 4)
    assert imp.parse_ofx_date("20260904") == date(2026, 9, 4)
    assert imp.parse_ofx_date("") is None
    assert imp.parse_ofx_date("20261301") is None


def test_an_ofx_that_is_not_ofx_says_so():
    result = imp.parse_ofx("hello, not a statement", source="x")
    assert result.rows == []
    assert result.warnings == ["no transactions found in the file"]


def test_parse_bytes_reads_what_a_sync_response_would_give():
    text = statement("statement_signed.csv").read_text(encoding="utf-8")
    result = imp.parse_bytes(text.encode("utf-8"), source="sync")
    assert result.source == "sync"
    assert result.read == 3


# ---------------------------------------------------------------- encoding


def test_cp1252_is_decoded(tmp_path):
    path = tmp_path / "latin.csv"
    path.write_bytes("Date,Description,Amount\n2026-09-01,CAFÉ LATTE,—4.50\n".encode("cp1252"))
    result = imp.parse_file(path)
    assert result.read == 1
    assert "CAF" in result.rows[0].description


# ----------------------------------------------------------------- fingerprint


def test_a_fingerprint_is_stable_across_rewording_of_the_same_row():
    args = (date(2026, 9, 22), -4.50, "CAFE LATTE 0091", "a_everyday", "september.csv")
    first = imp.fingerprint(*args)
    assert first == imp.fingerprint(*args)
    # Case and whitespace are normalised away; the amount is rounded to the cent.
    assert first == imp.fingerprint(date(2026, 9, 22), -4.5, "  cafe   latte 0091 ",
                                    "a_everyday", "september.csv")


def test_the_disambiguator_separates_two_otherwise_identical_rows():
    args = (date(2026, 9, 22), -4.50, "CAFE LATTE 0091", "a_everyday", "september.csv")
    assert imp.fingerprint(*args, index=0) != imp.fingerprint(*args, index=1)


def test_the_account_and_source_are_part_of_identity():
    base = (date(2026, 9, 22), -4.50, "CAFE LATTE 0091")
    assert imp.fingerprint(*base, "a", "s.csv") != imp.fingerprint(*base, "b", "s.csv")
    assert imp.fingerprint(*base, "a", "s.csv") != imp.fingerprint(*base, "a", "t.csv")


# --------------------------------------------------------------------- dedupe


def test_planning_an_import_of_a_fresh_file_marks_everything_new():
    parsed = imp.parse_file(statement("statement_signed.csv"))
    plan = imp.plan_import([], parsed.rows, source=parsed.source, account="a_everyday")

    assert len(plan.new) == 3
    assert plan.duplicates == []
    assert plan.imports_nothing is False


def test_reimporting_the_same_file_is_entirely_duplicate():
    parsed = imp.parse_file(statement("statement_signed.csv"))
    existing = [
        Transaction(id=f"t{i}", date=r.date, amount=r.amount, description=r.description,
                    account="a_everyday", fingerprint=imp.fingerprint(
                        r.date, r.amount, r.description, "a_everyday", parsed.source))
        for i, r in enumerate(parsed.rows)
    ]

    plan = imp.plan_import(existing, parsed.rows, source=parsed.source, account="a_everyday")
    assert plan.new == []
    assert len(plan.duplicates) == 3


def test_two_identical_rows_in_one_batch_are_both_imported():
    rows = [
        imp.RawTxn(date=date(2026, 9, 22), amount=-4.50, description="CAFE LATTE"),
        imp.RawTxn(date=date(2026, 9, 22), amount=-4.50, description="CAFE LATTE"),
    ]
    plan = imp.plan_import([], rows, source="s.csv", account="a")
    assert len(plan.new) == 2
    assert plan.new[0].fingerprint != plan.new[1].fingerprint


def test_a_third_identical_row_is_the_only_new_one_after_the_first_two():
    rows = [imp.RawTxn(date=date(2026, 9, 22), amount=-4.50, description="CAFE LATTE")
            for _ in range(2)]
    first = imp.plan_import([], rows, source="s.csv", account="a")
    existing = [Transaction(id="t0", date=rows[0].date, amount=-4.50,
                            description="CAFE LATTE", account="a",
                            fingerprint=first.new[0].fingerprint)]

    second = imp.plan_import(existing, rows, source="s.csv", account="a")
    # The batch's first row matches what is stored; the second is genuinely new.
    assert len(second.duplicates) == 1
    assert len(second.new) == 1


def test_a_row_with_an_external_id_dedupes_on_that_id():
    parsed = imp.parse_file(statement("statement.ofx"))
    existing = [Transaction(id="t0", date=parsed.rows[0].date,
                            amount=parsed.rows[0].amount,
                            description=parsed.rows[0].description,
                            account="a", external_id=parsed.rows[0].external_id)]

    plan = imp.plan_import(existing, parsed.rows, source=parsed.source, account="a")
    assert len(plan.duplicates) == 1
    assert plan.duplicates[0].reason == "external id already imported"
    assert len(plan.new) == 4


def test_an_external_id_beats_a_matching_fingerprint():
    """The aggregator's id is identity; nothing else gets a vote."""
    row = imp.RawTxn(date=date(2026, 9, 1), amount=-520.0, description="RENT",
                     external_id="abc123")
    # Same fingerprint the row would have had, but no external id: a different row.
    existing = [Transaction(id="t0", date=row.date, amount=row.amount,
                            description=row.description, account="a",
                            fingerprint=imp.fingerprint(row.date, row.amount,
                                                        row.description, "a", "s.csv"))]
    plan = imp.plan_import(existing, [row], source="s.csv", account="a")
    assert len(plan.new) == 1
    assert plan.new[0].fingerprint is None


def test_the_fingerprint_is_only_compared_within_one_source():
    """§21 scopes it to a source, so the same month twice from two sources shows up."""
    row = imp.RawTxn(date=date(2026, 9, 1), amount=-520.0, description="RENT")
    first = imp.plan_import([], [row], source="a.csv", account="acc")
    existing = [Transaction(id="t0", date=row.date, amount=row.amount,
                            description=row.description, account="acc",
                            fingerprint=first.new[0].fingerprint)]

    same_source = imp.plan_import(existing, [row], source="a.csv", account="acc")
    other_source = imp.plan_import(existing, [row], source="b.ofx", account="acc")

    assert same_source.new == []
    # The same month arriving from a different source is a row this app has not seen,
    # which is the conservative reading: a visible duplicate beats a silently dropped one.
    assert len(other_source.new) == 1
