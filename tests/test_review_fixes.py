"""Regression tests for defects found by an independent review.

Each test here exists because the code was wrong once, in a way that was not obvious and
would not have been caught by a passing suite. The header of each says what it guards.
"""

import json
from datetime import date

import pytest

from finance_tool.engine import analytics as an
from finance_tool.engine import categorise as cat
from finance_tool.engine import detect as det
from finance_tool.engine import importers as imp
from finance_tool.engine import ledger as led
from finance_tool.engine import recurrence as rec
from finance_tool.engine import sync as sy
from finance_tool.store.entities import (
    Account,
    Goal,
    Item,
    Rule,
    StoreDocument,
    Transaction,
    EXPENSE,
    INCOME,
)
from finance_tool.store.store import Store, StoreError, atomic_write_json

from fixtures import TODAY, build_document, build_store

TODAY_AT = __import__('datetime').datetime(2026, 10, 7, 12, 0, 0)

OCTOBER = (date(2026, 10, 1), date(2026, 10, 31))


# --------------------------------------------------------- recategorisation (§24)


def test_re_running_categorisation_does_not_lose_a_hand_set_category():
    """Guarded: the Settings button reset every hand-set category to a rule's answer, or
    to nothing, because `overwrite=True` did not distinguish a hand-edit from a rule's."""
    store = build_store()
    store.view().transaction("t_11").category = "Something I chose"
    store.view().transaction("t_11").rule_id = None

    cat.recategorise_all(store, rules=store.doc.rules)

    assert store.view().transaction("t_11").category == "Something I chose"


def test_a_rule_the_user_edited_still_takes_effect_on_its_own_rows():
    store = build_store()
    store.view().rule("r_rent").category = "Housing"

    cat.recategorise_all(store, rules=store.doc.rules)

    assert store.view().transaction("t_01").category == "Housing"


# ------------------------------------------------------------------ goals (§14-16)


def test_a_linked_goal_does_not_take_money_away_from_the_unlinked_ones():
    """Guarded: the linked amount was subtracted from the surplus twice — `monthly_surplus`
    already excludes the linked savings transfer as an expense — so it vanished."""
    from finance_tool.engine import goals as gl

    store = Store(doc=StoreDocument(
        items=[
            Item(id="d_in", name="Income", type=INCOME, amount=5000.0,
                 start=date(2026, 1, 1), recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=1"}),
            Item(id="d_rent", name="Rent", type=EXPENSE, amount=1000.0,
                 start=date(2026, 1, 1), recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=1"}),
            Item(id="d_save", name="Savings", type=EXPENSE, amount=500.0,
                 start=date(2026, 1, 1), recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=2"}),
        ],
        goals=[Goal(id="g_a", name="Linked", target=6000.0, saved=0.0,
                    linked_def_ids=["d_save"]),
               Goal(id="g_b", name="Unlinked", target=1000.0, saved=0.0)],
    ), data_dir=None)
    view = store.view()

    surplus = gl.monthly_surplus(view)
    rates = gl.contributions(view)

    assert surplus == pytest.approx(3500.00)          # 5000 - 1000 - 500
    assert rates["g_a"] == pytest.approx(500.00)
    # The linked 500 is already out of the surplus, so the whole of it is available.
    assert rates["g_b"] == pytest.approx(3500.00)


# --------------------------------------------------- column detection (§20)


def test_a_preamble_summary_line_does_not_poison_column_detection():
    """Guarded: a line like "Period Start, 01/09/2026, Opening Balance, 1,234.56" carries
    a bare date and bare numbers, so it passed the "data starts here" test and then won
    the amount column for itself — making every real row below it unreadable."""
    text = (
        "Period Start,01/09/2026,Opening Balance,1,234.56\n"
        "01/09/2026,RENT PAYMENT 8821,520.00\n"
        "05/09/2026,INTERNET PROVIDER 4471,79.00\n"
        "20/09/2026,MAGAZINE SUB 3312,15.00\n"
    )
    result = imp.parse_csv(text, source="preamble.csv")

    assert result.header_row is None
    descriptions = [row.description for row in result.rows]
    assert "RENT PAYMENT 8821" in descriptions
    assert "INTERNET PROVIDER 4471" in descriptions
    assert "MAGAZINE SUB 3312" in descriptions
    assert result.read >= 3


def test_a_genuinely_unreadable_row_still_does_not_cost_the_good_ones():
    """The counter-case to the fix above: skipping a preamble line is only worth doing
    when keeping it *costs* rows. A junk row in the middle must not make the rows above
    it disappear."""
    text = (
        "2026-09-01,RENT PAYMENT 8821,-520.00\n"
        "2026-09-02,NOT A DATE ROW,\n"
        "2026-09-05,INTERNET PROVIDER 4471,-79.00\n"
    )
    result = imp.parse_csv(text, source="middle-junk.csv")

    assert result.read == 2
    assert len(result.skipped) == 1
    assert [row.description for row in result.rows] == ["RENT PAYMENT 8821",
                                                        "INTERNET PROVIDER 4471"]


def test_a_semicolon_statement_whose_descriptions_contain_commas_still_reads():
    """Guarded: the delimiter was the character appearing most often in the file, so a
    semicolon export with commas inside its descriptions read as comma-delimited and
    imported as *zero* rows — a whole statement discarded, reported as "rows skipped"."""
    text = (
        "Date;Description;Amount\n"
        "2026-09-01;COLES, NORTHLAND, VIC;-12,34\n"
        "2026-09-02;UBER, TRIP 1, SYDNEY;-8,50\n"
        "2026-09-03;WOOLWORTHS, CHATSWOOD;-45,00\n"
    )

    assert imp.sniff_delimiter(text) == ";"
    result = imp.parse_csv(text, source="semis.csv")

    assert result.read == 3
    assert result.skipped == []
    assert [row.description for row in result.rows] == [
        "COLES, NORTHLAND, VIC", "UBER, TRIP 1, SYDNEY", "WOOLWORTHS, CHATSWOOD"]


def test_the_delimiter_is_chosen_by_what_it_splits_not_by_how_often_it_appears():
    assert imp.sniff_delimiter("Date,Description,Amount\n2026-09-01,RENT,5.00\n") == ","
    assert imp.sniff_delimiter("Date\tDescription\tAmount\n2026-09-01\tRENT\t5.00\n") == "\t"
    assert imp.sniff_delimiter("Date|Description|Amount\n2026-09-01|RENT|5.00\n") == "|"
    # A comma file that happens to contain semicolons keeps its commas.
    mixed = "Date,Description,Amount\n2026-09-01,A; B STORE,10.00\n2026-09-02,RENT,520.00\n"
    assert imp.sniff_delimiter(mixed) == ","
    assert len(imp.parse_csv(mixed).rows) == 2
    # Nothing splits a single-column file, so the old default stands.
    assert imp.sniff_delimiter("RENT PAYMENT\nCAFE LATTE\n") == ","


def test_a_read_only_load_of_an_unreadable_store_reports_rather_than_raising(tmp_path):
    """Guarded: the read-only path could reach a damaged store without the guard the
    normal path has, so a screenshot against one crashed instead of reporting."""
    from fixtures import corrupt_store_dir

    target = corrupt_store_dir(tmp_path)
    before = target.read_bytes()

    store = Store.load(tmp_path, create=False)

    assert any("could not be read" in note for note in store.notes)
    assert store.doc.items == []
    assert target.read_bytes() == before          # left exactly as it was
    assert not (tmp_path / "backups").exists()


# --------------------------------------------------------------- detection (§17)


def test_the_renewal_timeline_ignores_a_node_that_holds_children():
    """Guarded: the timeline looped every item while every other plan total loops leaves,
    so a node and its child were both counted — two figures for one plan."""
    store = Store(doc=StoreDocument(items=[
        Item(id="d_node", name="Bills", type=EXPENSE, amount=0.0,
             start=date(2026, 1, 1), recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=1"}),
        Item(id="d_child", name="Electric", type=EXPENSE, amount=145.0,
             parent_id="d_node", start=date(2026, 1, 1),
             recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=1"}),
    ]), data_dir=None)
    view = store.view()

    renewals = det.upcoming_renewals(view, TODAY, days=60)
    # The same window through the ledger — the two must agree about the same plan.
    rows = led.ledger_rows(view, (TODAY, TODAY + __import__("datetime").timedelta(days=60)),
                           kind="expense")

    assert {occ.name for occ in renewals} == {"Electric"}
    assert [occ.date for occ in renewals] == [occ.date for occ in rows]
    assert len(renewals) == 2                   # November and December


# ---------------------------------------------------------------------- sync (§22)


def test_a_synced_row_with_an_unreadable_date_is_counted_not_lost():
    """Guarded: rows whose `posted` was unreadable were filtered out with no count and no
    warning — a dropped transaction that looked like a successful sync."""
    store = Store(doc=StoreDocument(
        accounts=[Account(id="a1", name="Everyday", kind="transaction", currency="AUD")],
    ), data_dir=None)
    store.settings.enable_bank_sync = True

    payload = {"accounts": [{"id": "acc", "name": "Everyday", "currency": "AUD",
                             "balance": "100.00", "transactions": [
                                 {"id": "ok", "posted": 1756684800, "amount": "-4.50",
                                  "description": "CAFE LATTE"},
                                 {"id": "bad", "posted": "not-a-date",
                                  "amount": "-9.00", "description": "MYSTERY"},
                             ]}]}

    class Stub:
        def request(self, method, url, *, data=None, headers=None):
            return sy.Response(status=200, body=json.dumps(payload).encode())

    result = sy.sync(store, access_url="https://u:p@bridge.example/sf", today=TODAY,
                     transport=Stub())

    assert result.ok is True
    assert result.imported == 1
    assert any("no readable date" in warning for warning in result.warnings)
    assert len(store.doc.transactions) == 1


# ---------------------------------------------------------------- imports (§21)


def test_a_duplicate_external_id_within_one_csv_batch_is_dropped():
    """Guarded: the OFX parser refused a repeated FITID but the CSV path let both rows in,
    so the same transaction was stored twice."""
    rows = [
        imp.RawTxn(date=date(2026, 9, 1), amount=-520.0, description="RENT",
                   external_id="x1"),
        imp.RawTxn(date=date(2026, 9, 1), amount=-520.0, description="RENT",
                   external_id="x1"),
    ]
    plan = imp.plan_import([], rows, source="s.csv", account="a")

    assert len(plan.new) == 1
    assert len(plan.duplicates) == 1
    assert plan.duplicates[0].reason == "external id already imported"

    store = Store(doc=StoreDocument(), data_dir=None)
    summary = imp.import_rows(store, rows, source="s.csv", account="a")
    assert summary.imported == 1 and summary.duplicates == 1


# ------------------------------------------------------------- analytics (§14)


def test_a_user_bucket_named_unallocated_is_added_to_not_overwritten():
    """Guarded: the balancing target replaced a user's own category of the same name."""
    store = build_store()
    store.add("transactions", Transaction(
        id="t_extra", date=date(2026, 10, 7), amount=-10.0,
        description="ODDS AND ENDS", account="a_everyday", category="Unallocated"))

    flow = an.cashflow_links(store.view(), OCTOBER)
    targets = dict(flow.targets)

    assert targets[an.UNALLOCATED] > 10.0        # the user's 10 plus the surplus


def test_a_weekly_items_monthly_equivalent_matches_what_fires():
    """Guarded: the rollup counted raw weekday numbers while the scheduler took them mod 7,
    so an out-of-range weekday made the two disagree."""
    defn = Item(id="d", name="x", type=EXPENSE, amount=1.0, start=date(2026, 1, 1),
                recurrence={"rrule": "FREQ=WEEKLY;BYDAY=MO,MO,MO"})
    assert rec.monthly_equiv(defn) == pytest.approx(1.0 * 1 * (52 / 12))

    daily = Item(id="d2", name="y", type=EXPENSE, amount=1.0, start=date(2026, 1, 1),
                 recurrence={"rrule": "FREQ=WEEKLY;BYDAY=TU,TH,SA"})
    assert rec.monthly_equiv(daily) == pytest.approx(1.0 * 3 * (52 / 12))


# --------------------------------------------------------------- writes (§37)


def test_a_failed_write_cleans_up_its_temp_file(tmp_path):
    """Guarded: a failed save left `store.json.tmp` behind, which made the next run look
    like it had crashed."""
    target = tmp_path / "store.json"
    target.mkdir()                       # a directory cannot be replaced by a file

    with pytest.raises(StoreError, match="could not save"):
        atomic_write_json(target, {"a": 1})

    assert list(tmp_path.glob("*.tmp")) == []


def test_a_locked_target_is_reported_as_a_locked_target(tmp_path):
    """Windows raises PermissionError when another program holds the file open; a raw
    traceback for that is no use to the person looking at it."""
    target = tmp_path / "store.json"
    target.mkdir()

    with pytest.raises(StoreError) as caught:
        atomic_write_json(target, {"a": 1})

    assert "close it and save again" in str(caught.value)


# ------------------------------------------------------- reconciliation (§23)


def test_importing_a_statement_settles_the_occurrences_it_paid(tmp_path):
    """Guarded: `reconcile` existed and was tested, but nothing ever called it — importing
    categorised and never marked anything paid.

    Written against the engine rather than through a frontend, because this is the *engine's*
    promise: a statement that came in through the importer settles the plan it paid for.
    """
    from pathlib import Path

    store = Store(doc=StoreDocument(
        accounts=[Account(id="a_everyday", name="Everyday", kind="transaction",
                          currency="AUD")],
        items=[Item(id="d_rent", name="Rent", type=EXPENSE, amount=520.00,
                    start=date(2026, 1, 1), recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=1"},
                    due="1", category="Rent")],
    ), data_dir=tmp_path)

    statement = Path(__file__).resolve().parent / "statements" / "statement_signed.csv"
    summary = imp.import_file(store, statement, account="a_everyday")
    assert summary.imported == 3

    # Importing does not settle anything by itself; reconciliation is the step that does,
    # and it has to be asked for over the span the statement covers.
    result = imp.reconcile(store.view(), start=date(2026, 10, 1), end=date(2026, 10, 31))
    changed = imp.apply_reconciliation(store, result)

    assert changed == 1
    assert store.view().item("d_rent").paid == [date(2026, 10, 1)]


def test_a_sync_that_settles_something_says_so_in_its_description():
    result = sy.SyncResult(ok=True, settled=3)
    assert "3 marked paid" in result.describe()


# ============================================================ money-safety review


def test_a_save_that_fails_does_not_leave_its_delete_behind(tmp_path):
    """Guarded: a failed save left `DELETE FROM` uncommitted on the connection, and the
    *next* commit — even `save_settings` — finished the job. Fourteen items went that way."""
    store = build_store(tmp_path)
    store.save()
    before = len(store.doc.items)
    assert before > 0

    store.doc.items.insert(0, Item(id="d_bad", name="Bad", type=EXPENSE,
                                   amount=object(), start=date(2026, 1, 1)))
    with pytest.raises(TypeError):
        store.save()
    store.save_settings()          # the commit that used to bake the partial delete in
    store.close()

    assert len(Store.load(tmp_path).doc.items) == before


def test_a_failed_save_leaves_the_database_usable(tmp_path):
    store = build_store(tmp_path)
    store.save()
    store.doc.items.insert(0, Item(id="d_bad", name="Bad", type=EXPENSE, amount=object(),
                                   start=date(2026, 1, 1)))
    with pytest.raises(TypeError):
        store.save()

    store.doc.items = [i for i in store.doc.items if i.id != "d_bad"]
    store.save()                    # the next save must still work
    store.close()

    assert Store.load(tmp_path).doc.items


def test_splitting_a_series_carries_the_category_across(tmp_path):
    """Guarded: the tail copied every field but `category`, so the ledger and the budget
    disagreed about the same plan — the one disagreement §1 forbids."""
    store = build_store()
    original = store.view().item("d_rent")
    original.category = "Housing"

    _head, tail = store.split_series_from("d_rent", date(2026, 12, 1))

    assert tail.category == "Housing"


def test_a_recurring_item_with_no_start_costs_nothing_per_month():
    """Guarded: a rule with no start schedules nothing, but the rollup still charged for it,
    so a plan that never fires appeared in the monthly cost."""
    never = Item(id="d_never", name="Never", type=EXPENSE, amount=50.0, start=None,
                 recurrence={"rrule": "FREQ=MONTHLY"})

    assert rec.count_in(never, date(2026, 1, 1), date(2026, 12, 31)) == 0
    assert rec.monthly_equiv(never) == 0.0


def test_a_boolean_round_trips_as_a_boolean(tmp_path):
    """Guarded: SQLite has no boolean, so `subscription` came back as 1 and the grid
    rendered it "1" where it means "yes"."""
    store = build_store(tmp_path)
    store.doc.items[0].subscription = True
    store.save()
    store.close()

    reloaded = Store.load(tmp_path).doc.items[0]

    assert reloaded.subscription is True
    assert isinstance(reloaded.subscription, bool)


def test_the_json_export_includes_the_preferences(tmp_path):
    """Guarded: it said "the whole store" and left out settings and canvases, so an export
    was missing the layout and every preference — half of what a store is."""
    store = build_store(tmp_path)
    store.add_canvas("Second")
    store.save()

    payload = json.loads(store.export_json(tmp_path / "out.json").read_text("utf-8"))

    assert payload["settings"]["currency"] == "AUD"
    assert [c["name"] for c in payload["settings"]["canvases"]] == ["Second"]


def test_a_second_window_cannot_silently_overwrite_the_first(tmp_path):
    """Guarded: the store writes the whole document, so two windows were last-writer-wins
    and the loser's transactions disappeared with no warning at all."""
    from finance_tool.store import db as dbm

    store = build_store(tmp_path)
    store.save()

    first = Store.load(tmp_path)
    second = Store.load(tmp_path)
    second.add("transactions", Transaction(id="t_second", date=date(2026, 10, 5),
                                           amount=-1.0, description="FROM WINDOW B"))
    second.save()

    with pytest.raises(StoreError, match="another window"):
        first.save()

    # And the refusal cost nothing: window B's row is still there.
    first.close()
    assert Store.load(tmp_path).view().transaction("t_second") is not None


def test_a_locked_store_is_reported_as_busy_and_not_quarantined(tmp_path, monkeypatch):
    """Guarded: `DatabaseError` covers "locked" as well as "corrupt", so a sync client
    holding the file for a second moved the user's live store aside and opened an empty
    one. `data/` lives inside OneDrive, so this was an ordinary Tuesday."""
    import sqlite3

    from finance_tool.store import db as dbm
    from fixtures import seed_store_dir

    monkeypatch.setattr(dbm, "LOCK_TIMEOUT", 0.05)
    seed_store_dir(tmp_path)

    blocker = sqlite3.connect(tmp_path / dbm.DB_NAME, timeout=0)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(dbm.StoreBusyError):
            Store.load(tmp_path)
    finally:
        blocker.rollback()
        blocker.close()

    # Nothing was renamed, and nothing was declared corrupt.
    assert (tmp_path / dbm.DB_NAME).exists()
    assert not list((tmp_path / "backups").glob("corrupt-*"))
    assert Store.load(tmp_path).doc.items


def test_the_recovery_note_only_claims_what_happened(tmp_path, monkeypatch):
    """Guarded: the note said "it has been moved" before the move was attempted, so a
    Windows sharing violation produced a message describing something that had not
    happened — leaving the user unable to tell whether they had lost anything."""
    import shutil

    from fixtures import corrupt_store_dir

    corrupt_store_dir(tmp_path)
    monkeypatch.setattr(shutil, "move",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("in use")))

    store = Store.load(tmp_path)

    assert any("could not be read" in note for note in store.notes)
    assert not any("moved" in note.split(";")[0] for note in store.notes)


def test_two_deliberate_revisions_in_the_same_second_do_not_collide(tmp_path):
    """Guarded: names are stamped to the second, so the second snapshot overwrote the
    first — the one thing a backup must never do."""
    from finance_tool.store import history as hist

    when = TODAY_AT
    backups = tmp_path / "backups"

    first = hist.unique_path(backups, when, "manual")
    backups.mkdir(parents=True, exist_ok=True)
    first.write_bytes(b"one")
    second = hist.unique_path(backups, when, "manual")

    assert first != second
    assert hist.parse_name(second.name)[1] == "manual"
