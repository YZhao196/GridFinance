"""File-level imports: records, account resolution, dedupe and the inbox (§19–§21)."""

from datetime import date, datetime

import pytest

from finance_tool.engine import intake
from finance_tool.store.entities import Account, Settings, StoreDocument
from finance_tool.store.store import Store, StoreError

from fixtures import CAMT053

NOW = datetime(2026, 11, 1, 12, 0, 0)


def _store(tmp_path, accounts=None):
    accounts = accounts if accounts is not None else [
        Account(id="a_1", name="Everyday", currency="AUD")]
    return Store(data_dir=tmp_path / "data",
                 doc=StoreDocument(accounts=list(accounts)), settings=Settings())


def test_import_path_records_the_file_and_updates_the_balance(tmp_path):
    store = _store(tmp_path)
    path = tmp_path / "oct.xml"
    path.write_text(CAMT053, encoding="utf-8")

    outcome = intake.import_path(store, path, now=NOW)

    assert outcome.status == "ok"
    assert outcome.account == "a_1"
    assert outcome.summary.imported == 2
    assert outcome.record_id

    record = store.doc.imports[0]
    assert record.source_type == "camt053"
    assert (record.rows_new, record.rows_duplicate, record.rows_skipped) == (2, 0, 0)
    assert record.span_start == date(2026, 10, 1)
    assert record.span_end == date(2026, 10, 3)
    assert record.imported_at == NOW

    assert store.doc.accounts[0].balance == 942.2
    assert store.doc.accounts[0].balance_as_of == date(2026, 10, 3)
    coles = next(t for t in store.doc.transactions if t.external_id == "REF1")
    assert coles.merchant == "Coles 1234"
    assert coles.value_date == date(2026, 10, 2)
    assert coles.import_id == record.id
    assert path.exists()          # not moved unless asked

    store.save()
    store.close()
    again = Store.load(tmp_path / "data")
    assert again.doc.imports[0].filename == "oct.xml"
    assert len(again.doc.transactions) == 2
    assert again.doc.accounts[0].balance == 942.2
    again.close()


def test_reimporting_the_same_file_changes_nothing(tmp_path):
    store = _store(tmp_path)
    path = tmp_path / "oct.xml"
    path.write_text(CAMT053, encoding="utf-8")
    intake.import_path(store, path, now=NOW)

    again = intake.import_path(store, path, now=NOW)

    assert again.status == "already_imported"
    assert again.summary is None
    assert len(store.doc.transactions) == 2
    assert len(store.doc.imports) == 1


def test_scan_inbox_imports_xml_and_csv_and_moves_both(tmp_path):
    store = _store(tmp_path)
    inbox = tmp_path / "inbox"
    processed = tmp_path / "processed"
    inbox.mkdir()
    (inbox / "oct.xml").write_text(CAMT053, encoding="utf-8")
    (inbox / "nov.csv").write_text(
        "Date,Amount,Description\n05/10/2026,-4.50,COFFEE\n", encoding="utf-8")

    outcomes = intake.scan_inbox(store, folder=inbox, processed_dir=processed)

    assert {outcome.status for outcome in outcomes} == {"ok"}
    assert not list(inbox.iterdir())
    assert len(list(processed.iterdir())) == 2
    assert len(store.doc.imports) == 2
    assert len(store.doc.transactions) == 3

    again = Store.load(tmp_path / "data")       # scan_inbox saved once
    assert len(again.doc.transactions) == 3
    again.close()


def test_an_explicit_account_still_checks_the_currency(tmp_path):
    store = _store(tmp_path)
    usd = CAMT053.replace("<Ccy>AUD</Ccy>", "<Ccy>USD</Ccy>").replace(
        'Ccy="AUD"', 'Ccy="USD"')
    path = tmp_path / "usd.xml"
    path.write_text(usd, encoding="utf-8")

    outcome = intake.import_path(store, path, account="a_1", now=NOW)

    assert outcome.status == "error"
    assert "USD" in outcome.error and "AUD" in outcome.error
    assert store.doc.transactions == []


def test_a_statement_without_a_currency_header_is_refused_by_its_entry_currency(tmp_path):
    store = _store(tmp_path)
    usd = CAMT053.replace("<Ccy>AUD</Ccy>", "").replace('Ccy="AUD"', 'Ccy="USD"')
    path = tmp_path / "usd.xml"
    path.write_text(usd, encoding="utf-8")

    outcome = intake.import_path(store, path, now=NOW)

    assert outcome.status == "error"
    assert "USD" in outcome.error


def test_a_statement_finds_its_account_by_the_masked_number(tmp_path):
    store = _store(tmp_path, [
        Account(id="a_1", name="Spending"),
        Account(id="a_2", name="Joint", masked="****5678"),
    ])
    unnamed = CAMT053.replace("<Nm>Everyday</Nm>", "")
    path = tmp_path / "oct.xml"
    path.write_text(unnamed, encoding="utf-8")

    outcome = intake.import_path(store, path, now=NOW)

    assert outcome.status == "ok"
    assert outcome.account == "a_2"


def test_a_statement_that_cannot_be_placed_is_refused(tmp_path):
    store = _store(tmp_path, [
        Account(id="a_1", name="Everyday"),
        Account(id="a_2", name="Savings"),
    ])
    path = tmp_path / "mystery.csv"
    path.write_text("Date,Amount,Description\n05/10/2026,-4.50,COFFEE\n",
                    encoding="utf-8")

    outcome = intake.import_path(store, path, now=NOW)

    assert outcome.status == "error"
    assert "choose one" in outcome.error
    assert store.doc.transactions == []

    explicit = intake.import_path(store, path, account="a_2", now=NOW)
    assert explicit.status == "ok"
    assert explicit.summary.imported == 1
    assert store.doc.transactions[0].account == "a_2"


def test_overlapping_statements_add_only_new_rows(tmp_path):
    store = _store(tmp_path)
    first = tmp_path / "oct.xml"
    first.write_text(CAMT053, encoding="utf-8")
    intake.import_path(store, first, now=NOW)

    second_text = (CAMT053
                   .replace("<AcctSvcrRef>REF1</AcctSvcrRef>",
                            "<AcctSvcrRef>REF3</AcctSvcrRef>")
                   .replace("<Dt>2026-10-01</Dt>", "<Dt>2026-10-05</Dt>"))
    second = tmp_path / "oct-nov.xml"
    second.write_text(second_text, encoding="utf-8")

    outcome = intake.import_path(store, second, now=NOW)

    assert outcome.status == "ok"
    assert outcome.summary.imported == 1
    assert outcome.summary.duplicates == 1
    assert len(store.doc.transactions) == 3


def test_a_statement_in_another_currency_is_refused_not_imported(tmp_path):
    store = _store(tmp_path)
    usd = CAMT053.replace("<Ccy>AUD</Ccy>", "<Ccy>USD</Ccy>").replace(
        'Ccy="AUD"', 'Ccy="USD"')
    path = tmp_path / "usd.xml"
    path.write_text(usd, encoding="utf-8")

    outcome = intake.import_path(store, path, now=NOW)

    assert outcome.status == "error"
    assert "USD" in outcome.error and "AUD" in outcome.error
    assert store.doc.transactions == []
    assert store.doc.accounts[0].balance == 0.0


def test_an_older_statement_does_not_overwrite_a_newer_balance(tmp_path):
    store = _store(tmp_path)
    october = tmp_path / "oct.xml"
    october.write_text(CAMT053, encoding="utf-8")
    intake.import_path(store, october, now=NOW)

    september = (CAMT053
                 .replace("<Dt>2026-10-01</Dt>", "<Dt>2026-09-01</Dt>")
                 .replace("<Dt>2026-10-02</Dt>", "<Dt>2026-09-02</Dt>")
                 .replace("<Dt>2026-10-03</Dt>", "<Dt>2026-09-03</Dt>")
                 .replace(">1000.00<", ">400.00<")
                 .replace(">942.20<", ">500.00<")
                 .replace("REF1", "REF8").replace("REF2", "REF9"))
    earlier = tmp_path / "sep.xml"
    earlier.write_text(september, encoding="utf-8")

    outcome = intake.import_path(store, earlier, now=NOW)

    assert outcome.status == "ok"
    assert store.doc.accounts[0].balance == 942.2
    assert store.doc.accounts[0].balance_as_of == date(2026, 10, 3)


def test_a_re_download_under_a_new_name_is_still_a_duplicate(tmp_path):
    store = _store(tmp_path)
    first = tmp_path / "oct.csv"
    first.write_text("Date,Amount,Description\n01/10/2026,-57.80,COLES\n",
                     encoding="utf-8")
    intake.import_path(store, first, now=NOW)

    second = tmp_path / "oct-redownload.csv"
    second.write_text(
        "Date,Amount,Description\n"
        "01/10/2026,-57.80,COLES\n"
        "05/10/2026,-4.50,COFFEE\n", encoding="utf-8")

    outcome = intake.import_path(store, second, now=NOW)

    assert outcome.summary.imported == 1
    assert outcome.summary.duplicates == 1
    assert len(store.doc.transactions) == 2


def test_a_camt_row_with_an_id_is_matched_by_a_later_csv_without_one(tmp_path):
    store = _store(tmp_path)
    camt_path = tmp_path / "oct.xml"
    camt_path.write_text(CAMT053, encoding="utf-8")
    intake.import_path(store, camt_path, now=NOW)

    csv_path = tmp_path / "oct.csv"
    csv_path.write_text("Date,Amount,Description\n01/10/2026,-57.80,COLES 1234\n",
                        encoding="utf-8")
    outcome = intake.import_path(store, csv_path, now=NOW)

    assert outcome.summary.imported == 0
    assert outcome.summary.duplicates == 1
    assert len(store.doc.transactions) == 2


def test_the_same_file_can_be_imported_into_a_different_account(tmp_path):
    store = _store(tmp_path, [
        Account(id="a_1", name="Everyday", currency="AUD"),
        Account(id="a_2", name="Savings", currency="AUD"),
    ])
    path = tmp_path / "oct.xml"
    path.write_text(CAMT053, encoding="utf-8")

    first = intake.import_path(store, path, account="a_1", now=NOW)
    second = intake.import_path(store, path, account="a_2", now=NOW)

    assert first.status == "ok"
    assert second.status == "ok"
    assert second.summary.imported == 2
    assert len(store.doc.transactions) == 4


def test_the_closing_balance_date_sets_balance_as_of(tmp_path):
    store = _store(tmp_path)
    with_date = CAMT053.replace(
        '<Bal><Tp><CdOrPrtry><Cd>CLBD</Cd></CdOrPrtry></Tp>',
        '<Bal><Tp><CdOrPrtry><Cd>CLBD</Cd></CdOrPrtry></Tp>'
        '<Dt><Dt>2026-10-31</Dt></Dt>')
    path = tmp_path / "oct.xml"
    path.write_text(with_date, encoding="utf-8")

    intake.import_path(store, path, now=NOW)

    assert store.doc.accounts[0].balance_as_of == date(2026, 10, 31)


def test_a_raised_import_error_is_recorded(tmp_path, monkeypatch):
    store = _store(tmp_path)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "oct.xml").write_text(CAMT053, encoding="utf-8")

    def explode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(intake, "import_path", explode)

    outcomes = intake.scan_inbox(store, folder=inbox,
                                 processed_dir=tmp_path / "processed")

    assert outcomes[0].status == "error"
    assert outcomes[0].record_id
    assert "boom" in outcomes[0].error
    assert store.doc.imports[0].status == "error"


def test_the_same_row_in_csv_and_ofx_is_a_duplicate(tmp_path):
    store = _store(tmp_path)
    csv_path = tmp_path / "oct.csv"
    csv_path.write_text("Date,Amount,Description\n01/10/2026,-57.80,COLES\n",
                        encoding="utf-8")
    intake.import_path(store, csv_path, now=NOW)

    ofx = ("OFXHEADER:100\n<OFX><BANKTRANLIST><STMTTRN>"
           "<DTPOSTED>20261001</DTPOSTED><TRNAMT>-57.80</TRNAMT>"
           "<NAME>COLES</NAME></STMTTRN></BANKTRANLIST></OFX>")
    ofx_path = tmp_path / "oct.ofx"
    ofx_path.write_text(ofx, encoding="utf-8")

    outcome = intake.import_path(store, ofx_path, now=NOW)

    assert outcome.summary.imported == 0
    assert outcome.summary.duplicates == 1
    assert len(store.doc.transactions) == 1


def test_an_oversized_csv_field_is_a_warning_not_an_exception(tmp_path):
    from finance_tool.engine import importers as imp

    path = tmp_path / "huge.csv"
    path.write_text("Date,Amount,Description\n01/10/2026,-1.00," + "A" * 200000,
                    encoding="utf-8")

    parsed = imp.parse_file(path)

    assert parsed.rows == []
    assert any("could not read the CSV" in warning for warning in parsed.warnings)

    store = _store(tmp_path)
    outcome = intake.import_path(store, path, now=NOW)
    assert outcome.status == "error"


def test_ntryref_is_not_used_as_a_global_identifier(tmp_path):
    store = _store(tmp_path)
    october = (CAMT053
               .replace("<AcctSvcrRef>REF1</AcctSvcrRef>", "<NtryRef>1</NtryRef>")
               .replace("<AcctSvcrRef>REF2</AcctSvcrRef>", "<NtryRef>2</NtryRef>"))
    november = (october.replace("2026-10-01", "2026-11-01")
                .replace("2026-10-02", "2026-11-02")
                .replace("2026-10-03", "2026-11-03"))
    first = tmp_path / "oct.xml"
    first.write_text(october, encoding="utf-8")
    second = tmp_path / "nov.xml"
    second.write_text(november, encoding="utf-8")

    assert intake.import_path(store, first, now=NOW).summary.imported == 2
    outcome = intake.import_path(store, second, now=NOW)

    assert outcome.summary.imported == 2
    assert outcome.summary.duplicates == 0
    assert len(store.doc.transactions) == 4


def test_a_failed_import_is_recorded_in_history(tmp_path):
    store = _store(tmp_path)
    path = tmp_path / "broken.xml"
    path.write_text("this is not a CAMT file", encoding="utf-8")

    outcome = intake.import_path(store, path, now=NOW)

    assert outcome.status == "error"
    assert outcome.record_id
    record = store.doc.imports[0]
    assert record.status == "error"
    assert record.error


def test_a_bad_file_does_not_stop_the_inbox(tmp_path):
    store = _store(tmp_path)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "bad.xml").write_text("nothing here", encoding="utf-8")
    (inbox / "good.xml").write_text(CAMT053, encoding="utf-8")

    outcomes = intake.scan_inbox(store, folder=inbox,
                                 processed_dir=tmp_path / "processed")

    assert {outcome.status for outcome in outcomes} == {"error", "ok"}
    assert (inbox / "bad.xml").exists()          # left for the user to fix
    assert not (inbox / "good.xml").exists()
    assert len(store.doc.imports) == 2


def test_a_file_that_keeps_failing_is_recorded_once(tmp_path):
    store = _store(tmp_path)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "bad.xml").write_text("nothing here", encoding="utf-8")

    intake.scan_inbox(store, folder=inbox, processed_dir=tmp_path / "processed")
    intake.scan_inbox(store, folder=inbox, processed_dir=tmp_path / "processed")

    errors = [record for record in store.doc.imports if record.status == "error"]
    assert len(errors) == 1
    assert (inbox / "bad.xml").exists()


def test_inbox_files_stay_put_when_the_save_fails(tmp_path, monkeypatch):
    store = _store(tmp_path)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "oct.xml").write_text(CAMT053, encoding="utf-8")
    processed = tmp_path / "processed"

    def boom():
        raise StoreError("another window has changed the store")

    monkeypatch.setattr(store, "save", boom)

    with pytest.raises(StoreError):
        intake.scan_inbox(store, folder=inbox, processed_dir=processed)

    assert (inbox / "oct.xml").exists()
    assert not processed.exists() or not list(processed.iterdir())
