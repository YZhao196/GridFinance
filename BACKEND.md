# Backend reference

The Qt-free half of the app: `store/`, `engine/` and `hooks/`.

**Status, 2026-10-08: the frontend has been removed to be redesigned.** What remains is the
backend, its tests (867 passing, in about eleven seconds, with no Qt installed at all), and
the two documents. References below to `ui/`, to `--shot` and to the widget catalogue
describe the *reference frontend that was deleted* — kept because they record decisions
worth reusing, not because that code is still here. Everything the backend promises is still
promised; only the thing that used to read it is gone.

Written 2026-10-07, against schema v1. This document is the contract a new frontend reads.

Read alongside `design-doc.md`, which is the specification. This document describes what
was *built*: the same decisions, plus the parts the spec left open, plus the traps that
cost time.

**Sizes.** `store` 1.9k lines / 8 files · `engine` 5.8k / 14 · `hooks` 1.3k / 5 ·
`tests` 10k / 22. **867 tests passing**, 1 skipped (a POSIX file-mode check that cannot run
on Windows). No `ui/`.

---

## 1. The one law

**The store and the engine import no Qt, at all.** Not "mostly", not "except one helper".
`tests/test_layering.py` enforces it two ways: it imports every module under `store/`,
`engine/` and `hooks/` in a **subprocess with PyQt6 blocked from the import system**, and
it greps the sources for the string `PyQt6`.

Three reasons, in order of how much they cost to lose:

1. **The engine is testable without a display.** Every number is produced by a function
   that takes data and returns data, so the whole backend is exercised in ~11 seconds with
   no window, no event loop and no flakiness. With the frontend removed this is no longer a
   nicety: it is the only way anything is tested.
2. **Screenshots were deterministic.** The reference frontend's `--shot` harness relied on
   there being no animation and no clock inside the engine, so the same store rendered the
   same pixels. A new frontend gets that property for free if it keeps the same rule.
3. **"The ledger says X but the chart says Y" becomes impossible.** There is one place
   each figure comes from, and the UI cannot compute a competing one — it has no access
   to the arithmetic.

Corollaries a frontend must respect:

- A UI file may not import another UI file's internals; shared behaviour moves down a layer.
- The UI reads through `StoreView`, never through `store.doc` directly (except where it is
  deliberately mutating an entity in place, see §4.6).
- No engine function takes a widget, a `QObject`, or a signal.
- No engine function reads the clock. `today` is always a parameter.

---

## 2. Package map

```
finance_tool/
  paths.py            where data lives: data/store.db, secrets.json, backups/
  demo.py             a small in-memory dataset for --demo and --shot. Never written to disk.
  store/              +--------------------------------------------------+
    entities.py       |  no Qt in this block, enforced by a test          |
    codec.py          |                                                  |
    db.py             |   store  ──▶  engine  ──▶  hooks  ──▶  ui        |
    history.py        |   entities   pure         WidgetData   Qt only   |
    migrations.py     |                                                  |
    secrets.py        |                                                  |
    store.py          |                                                  |
  engine/             +--------------------------------------------------+
    rrule.py       recurrence.py  periods.py   ledger.py   analytics.py
    goals.py       shared.py      tracker.py   detect.py
    categorise.py  search.py      importers.py sync.py
  hooks/
    types.py       HookContext, WidgetData, Row
    templates.py   ratio, share_of, delta, cumulative, rolling_mean, scale, combine
    cache.py       memoisation keyed on store.version
    library.py     the 35 hooks the widget catalogue calls
  ui/               the only package that imports PyQt6
```

Dependency direction is one-way and does not cycle:

```
paths ← store ← engine ← hooks ← ui
```

`importers` and `sync` are the **only** two engine modules allowed to touch the filesystem
or a socket. Every other engine module is pure.

---

## 3. Data model

### 3.1 The definition / occurrence split

This is the central decision everything else leans on.

A **definition** is a plan item: "Rent, $520, monthly, from 2026-01-01, open-ended".
An **occurrence** is that definition landing on a date: "Rent, 2026-11-01".

```
Item (id="d_rent", amount=520.00, recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=1"})
  ├── occurrence 2026-09-01   paid
  ├── occurrence 2026-10-01   paid
  ├── occurrence 2026-11-01   ← overridden to 545.00
  └── occurrence 2026-12-01
```

**Occurrences are never stored as rows.** They are derived on demand. The only
per-occurrence state that is stored is the exception:

| Field on `Item` | Type | Meaning |
|---|---|---|
| `overrides` | `dict[date, dict[str, Any]]` | This occurrence differs — `amount`, `note`, `due`, `priority`, `name`, `tags` |
| `cancelled` | `list[date]` | This occurrence does not happen |
| `paid` | `list[date]` | This occurrence has been settled |
| `shares_paid` | `dict[date, list[str]]` | Per person, per occurrence: who has settled their share |

Two functions resolve everything, and every consumer goes through them:

```python
occurrence_dates(defn, start, end, *, include_cancelled=False) -> list[date]
effective(defn, occ, *, cancelled=None) -> Occurrence
```

`occurrences()` is their composition (dates → resolved rows); `all_occurrences()` does it
across many definitions and sorts. There is no third way to ask what happens in a month.

**`effective()` applies the override; the definition is never mutated by it.** That is why
the ledger can show a plan forward for years without rewriting history, and why
"change next month's rent" is a one-key edit.

**Cancelled occurrences are excluded from money by default and included as rows on
request.** `occurrence_dates(...)` omits them; `include_cancelled=True` returns them so a
ledger can render the row struck through. Every total is computed with them excluded.

### 3.2 The two sign conventions

| | Stored as | Direction comes from |
|---|---|---|
| `Item.amount` | **positive** | `Item.type` (`"income"` / `"expense"`) |
| `Transaction.amount` | **signed** | itself — negative is money out |

`store.entities.signed(node)` is the only place they meet. Use it rather than reading
`amount` directly on anything whose kind you are unsure of.

### 3.3 Entities

All are plain mutable dataclasses in `store/entities.py`, all constructible from a dict
via `X.from_dict(encode(x))` and serialisable via `x.to_dict()`.

**`Item`** — a plan definition.

```python
Item(id, parent_id=None, name="", type="expense", amount=0.0,
     start=None, end=None,                 # date | None; end=None means open-ended
     recurrence=None,                      # see §5.1
     due=None,                             # "1" (day of month) | ISO date | None
     priority=0, tags=[], note="",
     category="",                          # the budget line this funds
     subscription=False,
     shared=None,                          # {"members": [person_id], "split": "even" | {id: amount}}
     overrides={}, cancelled=[], paid=[], shares_paid={},
     ideal=None, cancel_by=None, expanded=False,
     created_at=None, updated_at=None)
```

Notes:
- `parent_id` gives nesting: a category node holding items, an item holding line items.
  **A node with children contributes no occurrence of its own** — see `leaf_items()`.
- `category` is not in the spec's field table for `Item`. It is required for
  budget-vs-actual to have a join key: transactions and rules both carry a `category`, and
  without one on the item there is nothing to group the plan by. See §9.
- `expanded` is UI-only state that lives in the domain because there is nowhere else for
  a per-row flag to persist.

**`Account`** — `id, name, kind, balance, currency, external_id, high_water`.
`high_water` is the SimpleFIN incremental mark (the last `posted` epoch seen), added for
§22 and not in the spec's table.

**`Transaction`** — `id, date, amount, description, account, category, tags, external_id,
fingerprint, source, imported_at, rule_id`.
`fingerprint` is the fallback identity for rows with no `external_id` (§7.2), and
`rule_id` records which rule claimed the row so a hand-edit can release it.

**`Snapshot`** — `date, value`. A net-worth reading. Whether these are captured
automatically is an open decision in the spec; the engine reports what exists.

**`Settings`** — preferences: `currency, lens, anchor, canvases, canvas_active,
enable_bank_sync, animate`. `lens` is one of `week|month|quarter|year`.

**`StoreDocument`** — `schema_version` plus eight lists: `items, accounts, transactions,
rules, goals, tracker, people, snapshots`.

**`PlacedWidget` / `Canvas`** — layout data: `widget, x, y, w, h, config` on a 12-column
grid. Layout is *data*, and the engine never reads it. A frontend may replace this
entirely; nothing in the backend depends on it.

### 3.4 Money is a float

Double precision is exact to well within a cent at any plausible personal scale, and
integer cents would have to be threaded through every analytic. **Totals are rounded only
at display** — `theme.format_money`, in the UI layer.

---

## 4. The store

### 4.1 Where data lives

```
data/                     gitignored, created on first run
  store.db                the whole domain, in SQLite
  secrets.json            sync credential only — never logged, never exported
  backups/                timestamped snapshots, written before any migration
```

One database rather than a file per entity, because the app's queries span entities: a
budget-vs-actual view reads items *and* transactions, and one database makes that read
atomic and cheap. Settings live in a row inside `store.db` rather than in their own file,
so there is one path rather than two and no question about which is live.

### 4.2 The schema, and why it is derived

`store/db.py` builds the tables from the entities' **own type hints**, so adding a field to
`entities.py` is still a one-line change — the same promise `codec.py` makes for JSON, and
not a hand-written schema that drifts the first time someone is in a hurry.

A field becomes a **real column** when it is a scalar worth sorting, filtering or joining
on — `name`, `amount`, `start`, `priority`, `subscription`, and `type`, which is a `Literal`
of strings and therefore a column rather than a structure. A `list` or `dict` becomes a
**JSON column**: `overrides`, `cancelled`, `paid`, `tags`, `shared` are per-occurrence bags
keyed by a date, and a table each would mean several joins for a query nobody writes.

**`user_version` is stamped once**, when the database is created, and never overwritten —
writing it on every open would silently relabel a store made by a newer build as this
build's.

**Occurrences are still derived, never stored.** That is the most important property of
this data model and the schema does not change it.

### 4.3 A store with no directory cannot write

`Store(doc=..., data_dir=None)` means **this store has no directory** — an in-memory
database — not "wherever the app keeps its data". `store_path` and `backups_dir` are
`None`, `Store.has_directory` is `False`, and `save()`, `save_settings()` and `backup()`
**raise `StoreError`** rather than quietly writing somewhere.

Only `Store.load(data_dir)` resolves a location, and the entry point passes it explicitly
(`--demo` and the default `--shot` get a `tempfile.mkdtemp()` registered with `atexit`, so
the dataset is fully usable, still disposable, and the directory is actually removed; the
store is closed before the removal, because on Windows an open database handle stops a
directory being deleted). The rule exists because the earlier default — falling back to the
app's real `data/` directory — let a throwaway store write into the user's data. A silent
write to the wrong place is exactly the failure this project keeps designing out, so it is
now a loud one.

**Reading must not be able to write, and SQLite is what enforces it.** `load(create=False)`
opens the file with `mode=ro` through a URI, so a write is refused by the engine rather
than by a flag this code remembers to check — the same reason the read-only tests assert on
`sqlite3.OperationalError` rather than on our own exception. It also does not create the
directory. The `--shot` path uses it, so a screenshot cannot create or edit the store it is
a picture of.

### 4.4 Saving is a transaction

`save()` writes the document — every row, then the settings and canvas order — and commits,
all in one SQLite transaction. **A failure rolls back.** Without the explicit `rollback()`, a
failed save left its `DELETE FROM` sitting uncommitted on the connection and the *next*
commit — even `save_settings` — finished the job; fourteen items went that way.

**A second window cannot silently overwrite the first.** `save()` compares a `generation`
counter in the `meta` table against the one this handle loaded with, and refuses if it has
moved. The store writes the whole document, so two windows are last-writer-wins and the
loser's entries vanish with no warning; one counter turns that into a refusal the user can
act on. `restore()` claims the store by bumping the counter itself.

It writes the **whole** document rather than only what changed, because the editors mutate
entities in place and then commit: the store has no way to know which rows moved. Per-row
writes become the point once a grid edits cells through `update()` one at a time; today the
benefit of SQLite is one file, real transactions, real read-only, and indexed queries.

`atomic_write_json(path, payload)` remains for **JSON exports** — the human-readable copy
taken from Settings. It serialises to `<path>.tmp`, `fsync`s, then `os.replace`s over the
target.

### 4.5 The store is versioned

`Store.version` is a monotonically increasing counter bumped on **every** write
(`save`, `save_settings`, and every mutator). The hook cache is keyed on it, so cache
invalidation is exact rather than time-based. There is no TTL anywhere and there should
never be one.

### 4.6 Loading, quarantine and migration

```python
Store.load(data_dir, *, clock=datetime.now, create=True) -> Store
```

`create=False` is the read-only load described in §4.3: it writes nothing at all, and steps
2 and 3 below report instead of acting. On load:

1. **Missing file** → an empty document; no error.
2. **A file another process is holding** → `StoreBusyError`, and **nothing is touched**.
   `sqlite3.DatabaseError` covers "locked" as well as "not a database", and the two need
   opposite responses: the store lives inside the user's OneDrive folder, so a sync client
   holding it for a second is an ordinary event. Reading that as corruption moved the live
   store aside and opened an empty one — the worst failure this app could have. The entry
   point catches it and prints a sentence rather than a traceback.
3. **A file that is not a database** → the file is **moved** aside to
   `backups/corrupt-store-<stamp>.db`, a note is appended to `store.notes`, and the app
   starts empty. *Moved*, not copied: unlike a bad JSON file, one left in place would fail
   every later open, so there would be nothing to recover from either. If the move itself
   fails — a sharing violation — the app **refuses to start** rather than carrying on, and
   says where the file is. A recovery note never claims a move that did not happen: a
   message describing something that did not occur leaves the user unable to tell whether
   they lost anything.
3. **`user_version` lower than this build** → `backup(reason="pre-migration")` first, then
   the registered migration steps. None are registered at v1; the machinery is tested with
   an injected table, and `store/migrations.py` keeps the pure `vN_to_vN1(doc) -> doc` walk
   for the JSON *export* shape.
4. **`user_version` newer than this build** → refused, file untouched, one note. The version
   is not rewritten, so the store is still readable by whatever wrote it.

`Store.notes` is the channel for "something needs saying". A frontend must surface it
(the reference UI shows it as a Notes section in Settings). Swallowing it is a bug.

`backup(reason)` uses SQLite's own online backup rather than copying the file, so a snapshot
is consistent even if taken mid-write, and a `.db` snapshot restores exactly.

`Store.close()` releases the file. Anything that removes a data directory has to call it
first — an open handle stops the directory being deleted on Windows.

### 4.7 Revision history

```python
store.revision(reason="auto", *, when=None, force=False) -> Path | None
store.revisions() -> list[Revision]
store.restore(snapshot) -> None
store.export_json(destination) -> Path
```

`data/backups/store-<stamp>-<reason>.db` holds snapshots of the database, taken through
SQLite's own online backup so each is consistent even mid-write, and restored *exactly* by
writing a snapshot's contents back into the live connection (never by copying files).

**Automatic snapshots are best-effort; deliberate ones are loud.** `revision()` with the
default reason is throttled to one per `history.AUTOMATIC_INTERVAL`, and a store with no
directory or no write access simply has no history — nobody asked for it. Any other reason
is an act, so it goes through `_require_writable` and is refused if it cannot happen.

**Retention never touches a deliberate snapshot.** `history.expired()` is a pure function
of the time and the revisions: keep everything from the last day, one an hour for a week,
one a day for a year, then drop. Only automatic snapshots are in its answer — the moment
you need a backup is not the moment to find the policy ate it.

**`restore()` takes a snapshot of the present first**, so a restore can itself be undone
from the History page. That is the whole reason the action is not frightening.

`export_json()` writes the human-readable copy — not the store, but the thing you can read,
diff or hand to someone. It is what `atomic_write_json` is still for.

### 4.8 Secrets

`data/secrets.json`, mode `0600` best-effort, holds `simplefin_access_url` and nothing
else. `Secrets.__repr__` prints key *names*, never values, so a credential cannot reach a
log through an f-string. `save_secrets` with nothing in it **deletes the file** — revoking
leaves no residue. There is no field for a bank password, and a test asserts that.

### 4.9 `Store` (writing) and `StoreView` (reading)

**Read through `StoreView`. Write through `Store`.** `StoreView` is a frozen dataclass
with no mutating method on it at all — a hook physically cannot write. There is a test
asserting `add`, `update`, `remove`, `save`, `set_override` and `mark_paid` do not exist
on it.

```python
view.items / accounts / transactions / rules / goals / tracker / people / snapshots
view.item(id) / account(id) / person(id) / rule(id) / goal(id) / transaction(id)
view.item_name(id) / person_name(id)
view.children_of(parent_id) / roots()
view.account_by_external(external_id)
view.foreign_accounts()          # currency ≠ the app's: flagged, never summed (§8)
view.settings / view.version / view.currency
```

`Store` adds the write path:

```python
store.add(collection, entity) / get / remove / update
store.add_item(item) / update_item(item) / delete_item(id, recursive=True)
# The per-occurrence API of §9 — the only way an occurrence's state changes:
store.set_override(item_id, occ, field, value)
store.clear_override(item_id, occ, field=None)
store.cancel_occurrence(item_id, occ) / uncancel_occurrence(...)
store.mark_paid(item_id, occ, paid=True)
store.mark_share_paid(item_id, occ, person_id, paid=True)
store.split_series_from(item_id, occ) -> (original, tail)
# Canvases:
store.add_canvas / rename_canvas / delete_canvas / reorder_canvas / canvas / active_canvas
# Persistence:
store.save()          # writes both files atomically and bumps version
store.backup(reason)  # copy the store aside; returns the path
```

`split_series_from` is the subtlest one: it ends the original the day before `occ` and
returns a **new** `Item` starting at `occ`, moving any occurrence state at or after `occ`
with the tail. The past is untouched. This is what "change it and everything after" does.

**Mutators bump `version` but do not write.** The caller decides when to `save()`, so a
popout that edits six fields writes once.

---

## 5. The engine

Four contracts hold everywhere (`design-doc.md` §15), and they are the reason the engine
can be trusted:

1. **Pure.** A function of its arguments and the store snapshot. No clocks, no globals, no
   I/O — except in `importers` and `sync`.
2. **Read-only over the store.** A query never mutates. (The `Store` mutators above are
   the explicit exception, and they are not engine functions.)
3. **No Qt.** See §1.
4. **Total.** Degenerate input returns an empty or zero result, **never raises**:
   no transactions, a zero denominator, a goal with `target = 0`, a range with no
   occurrences, an inverted date range. If you find an engine function that raises on
   empty input, that is a bug.

### 5.1 `recurrence` — occurrence generation

```python
occurrence_dates(defn, start, end, *, include_cancelled=False) -> list[date]
effective(defn, occ, *, cancelled=None) -> Occurrence
occurrences(defn, start, end, *, include_cancelled=False) -> list[Occurrence]
all_occurrences(defns, start, end, *, include_cancelled=False) -> list[Occurrence]
next_occurrence(defn, after, *, inclusive=False) -> date | None
next_occurrence_of(defns, after) -> Occurrence | None
monthly_equiv(defn, *, amount=None) -> float        # per month; a one-off is 0.0
yearly_equiv(defn) -> float
fires_in(defn, start, end) -> bool
count_in(defn, start, end, *, include_cancelled=False) -> int
recurrence_kind(defn) -> str
rule_of(defn) -> RRule | None
sort_key(occ) -> tuple
clamp_day(year, month, day) -> date
add_months(anchor, months) -> date
```

A definition's schedule is an **RFC 5545 recurrence rule** stored as text under
`recurrence = {"rrule": "FREQ=MONTHLY;BYDAY=2TU"}`. `recurrence = None` (or a rule with no
usable `FREQ`) is a one-off. Supported parts: `FREQ`, `INTERVAL`, `BYDAY` (ordinals such as
`2TU` and `-1FR`), `BYMONTHDAY` (negatives count back from the end), `BYMONTH`,
`BYSETPOS`, `COUNT`, `UNTIL`, `WKST`. Time-of-day and week-of-year parts are parsed and
recorded in `RRule.ignored` rather than refused — a *query* may never fail (§15.4), so an
unreadable rule degrades to a one-off; the **editor** is what refuses, so a typing mistake
never becomes a silent one-off.

The evaluator is `engine/rrule.py` — pure, no Qt, and imported only by `engine/recurrence.py`,
which keeps every public name above and remains the only way anything asks what happens in a
month.

**Month-end clamping is a named rule, not an accident, and it is a deliberate deviation from
RFC 5545.** The standard gives `BYMONTHDAY=31` *no* February occurrence; here it fires on
Feb 28 (29 in a leap year) and Apr 30, so a monthly item on the 31st fires **twelve times a
year** — never skipped, never rolled into the following month. `clamp_day` is the only place
this happens. The same clamping applies to a `BYDAY` ordinal past the end of a month (`5FR`
in a four-Friday month is that month's last Friday) and to a day used as a filter.

**Intervals are anchored, not iterative.** A rule advances by whole periods from `DTSTART`
(`Item.start`), so a monthly interval from the 31st lands on the 31st again in March rather
than drifting to the 28th after passing through February. A recurring item with **no** start
date therefore schedules nothing: there is no phase to anchor to. (It used to take its phase
from the range being asked about, which made the answer depend on the question.)

**`monthly_equiv` is derived from the rule, never counted over a window.** Counting would
give a weekly rule 52 *or* 53 occurrences depending on where the window opened, so the rollup
and the scheduler could disagree about the same plan. `RRule.rate_per_month()` gives the
steady-state rate in closed form for every shape that has one — which is what makes the
existing figures exactly reproducible — and falls back to counting over one 400-year
Gregorian cycle (exactly 20,871 weeks) for the few shapes that do not.

**`monthly_equiv` returns `0.0` for a one-off.** A concert ticket is not a monthly cost,
and averaging it in would understate every subscription on the canvas.

**Ordering is a total order**: `sort_key = (due, priority, name.casefold(), item_id)`. The
id is the final tiebreaker so two same-named, same-priority, same-due items never shuffle
between renders.

`Occurrence` is a frozen dataclass: `item_id, date, name, type, amount (unsigned), due
(a real date), priority, tags, note, subscription, shared, paid, cancelled, definition`.
It carries `.signed_amount`, `.is_overdue(today)`, `.is_due_soon(today, within=7)`.

### 5.2 `periods` — lenses and ranges

A **lens** is the window the whole app looks through: `LENSES = ("week", "month",
"quarter", "year")`.

```python
range_for_lens(lens, anchor) -> (date, date)     # inclusive at both ends
step_lens(lens, anchor, steps) -> (date, date)
previous_range(lens, span) / next_range(lens, span) / lens_containing(lens, span)
normalise_lens(lens) -> str                      # an unknown lens is a month, never an error
sub_ranges(lens, start, end) -> list[span]       # tiles the range at the given resolution
month_ranges / month_starts / week_ranges / each_day / month_grid
label_for(lens, span) -> str                     # "October 2026", "Q4 2026", "5–11 Oct 2026"
day_count_elapsed(span, today) -> int            # for pace; clamped, never negative
contains / clamp_range / days_in
```

Two things worth knowing:

- `sub_ranges` **tiles and clips**. Asking for a year of weeks gives ~52 bars; a week
  that straddles the boundary is clipped, so summing the bars gives exactly the period's
  total and never borrows from a neighbouring month.
- `week_ranges` is Monday-first, matching `iso_week_range`.

### 5.3 `ledger` — totals, summaries, budgets

```python
leaf_items(items) / is_leaf(items, item) / subtree(items, root_id)
totals_in_range(items, start, end, *, leaves_only=True) -> Totals
plan_totals(view, span, *, items=None) -> Totals
actual_totals(view, span) -> Totals
totals_for_transactions(transactions, start, end) -> Totals
totals_for_occurrences(occurrences) -> Totals
ledger_rows(view, span, *, kind="expense", include_cancelled=False) -> list[Occurrence]
ledger_groups(view, span, ...) -> list[LedgerGroup]     # children under their parent node
plan_summary(view, span) -> PlanSummary
budget_vs_actual(view, span, *, today=None) -> list[BudgetRow]
budget_coverage(view, span=None, *, expenses_only=True) -> Coverage
uncategorised_transactions(view, span=None) -> list[Transaction]
bills_in_range(view, start, end, *, include_paid=False) -> list[Occurrence]
overdue_bills(view, today, *, lookback_days=180) / due_soon_bills(view, today, *, days=7)
due_summary(bills, today, *, soon_days=7) -> DueSummary
next_due(view, today) -> Occurrence | None
subscription_totals(view, *, when) -> SubscriptionTotals
monthly_plan_by_category(items) -> dict[str, float]
predicted_income(view, span) -> IncomeProjection
active_on(item, when) -> bool
```

`Totals` is `income, expenses` (both **positive magnitudes**), `count, income_count,
expense_count`; `.net = income - expenses`; `.savings_rate` is `None` when there is no
income rather than a division error. It also has `.plus()`, `.delta_from()`.

Four decisions to keep in mind:

- **`leaves_only=True` by default.** A category node with children is a *group*, not
  something that fires on a date. Without this a node like "Utilities" contributes one
  zero-amount occurrence on its own start date and inflates every count in the app.
- **A cancelled occurrence is excluded from the money and present in the rows.**
  `plan_summary.occurrences` counts scheduled rows *including* cancelled ones, so it
  matches what the ledger renders; `plan_summary.plan` excludes them, so the money is
  right.
- **`budget_vs_actual` plans only over items that name a category.** A budget is a *named*
  category, so an uncategorised expense is not a budget line. Uncategorised *spend* still
  gets a row of its own — dropping it would let a third of real spend leave a budget view
  unremarked (§23/§24).
- **`budget_coverage` measures by count *and* by dollar value**, because a count-only bar
  can pass while the largest transactions sit in Uncategorised. §24's acceptance target is
  ≥80% on both.

### 5.4 `analytics` — history, summaries, projection

```python
history(view, start, end, *, lens="month", net_worth_points=None) -> list[HistoryPoint]
period_summary(view, span, *, lens="month", window_months=12) -> PeriodSummary
metrics(view, span, *, months=12, lens="month") -> Metrics
records(points) -> Records
savings_record(points) -> SavingsRecord
month_over_month(view, span, *, lens="month", periods=6) -> list[HistoryPoint]
forecast(view, span, *, lens="month", history_months=12) -> Forecast
forecast_band(view, span, *, lens="month", history_months=12, confidence=1.0) -> Forecast
net_worth(view) / liquid_balance(view) -> float
net_worth_series(view, start=None, end=None, *, today=None) -> NetWorthSeries
cashflow_links(view, span, *, basis="plan") -> Cashflow
spend_by_category(view, span, *, basis="actual", limit=0) -> list[(str, float)]
spend_by_tag(view, span, *, limit=0) -> list[(str, float)]
daily_spend(view, span) -> list[(date, float)]        # zero-filled: a calendar needs zeros
```

- **Every `HistoryPoint` carries plan and actual for the same span.** Nothing here can
  quietly compare this month's plan to last month's bank statement.
- **A trend with one point has no trend.** Series are returned as they are, however short.
  Suppressing a one-point sparkline is the frontend's job; the number is still true.
- **`Records` reports the sign, not a rank.** `best`/`worst`/`months_positive` — the
  frontend colours by sign, never by rank (§29).
- **`Forecast` is straight-line on the plan**, deliberately: what the plan *implies*.
  `forecast_band` adds a half-width of `sigma · sqrt(periods_ahead)`, where `sigma` is the
  observed month-to-month gap between plan and actual. With fewer than two months of
  actuals there is nothing to measure, so the fallback is 15% of planned monthly spend —
  a wide band, because a guess should look like one.
- **`net_worth` sums every account in the app's currency; `liquid_balance` only spendable
  kinds.** A debt is an account with a negative balance. Foreign-currency accounts are in
  neither.
- **`cashflow_links` balances.** Sources are income plus (if needed) a `Deficit` source;
  targets are spending categories plus an `Unallocated` target. Allocation is
  **proportional** — a stated convention, not a discovered fact — and with the balancing
  term in place each source's links sum to its income *and* each target's links sum to its
  amount. A Sankey that does not balance is not drawable.

### 5.5 `goals`

```python
monthly_income(view) / monthly_expenses(view) / monthly_surplus(view) -> float
linked_monthly(view, goal) -> float
contributions(view) -> dict[goal_id, float]
goal_eta(view, goal, today, *, monthly=None) -> GoalProjection
goal_projections(view, today) -> list[GoalProjection]
savings_rate(view, span) -> float | None
savings_record(view, today) -> SavingsRecord
goal_for_item(view, item_id) -> list[Goal]
```

Where the contribution comes from is the whole design question, and it is resolved in
`contributions()`:

- A **linked** goal is funded by *those* items' monthly equivalents.
- An **unlinked** goal shares the **whole monthly surplus**, in proportion to what the
  goals still need — so two of them cannot both claim the same dollar.
- A deficit plan funds no unlinked goal.

The unlinked pool is the whole surplus and not "the surplus minus the linked
commitments": `monthly_surplus` is income minus every leaf expense, and a linked goal's
savings transfer *is* one of those expenses, so it is already out of the figure.
Subtracting it again made the linked amount vanish from the plan entirely.

`goal_eta` walks the **schedule** for a linked goal, so the goal completes on the date the
money actually lands. That matters for two shapes a rate alone gets wrong: a goal funded
by a single one-off payment has a monthly equivalent of zero but a real completion date,
and a goal whose linked item ends before the target is reached has a rate that says
"eleven more payments" and a schedule that says "never". `eta is None` means not on track,
never "unknown".

### 5.6 `shared` — splits and settle-up

```python
split_members(defn) / is_shared(defn) -> ...
member_shares(defn, occ) -> dict[person_id, float]
user_share(defn, occ) -> float
settled_members / unpaid_members(defn, occ) -> list[person_id]
all_paid(defn, occ) -> bool
occurrence_outstanding(defn, occ) -> float
share_rows(view, start, end, *, include_paid=True) -> list[ShareRow]
people_roster(view, span) -> list[PersonBalance]
who_owes(view, span) -> dict[person_id, float]
owed_summary(view, span, *, top=3) -> OwedSummary
settle_up(view, span) -> dict[str, float] / all_settled(view, span) -> bool
default_span(view, today, *, ahead_days=0) -> (date, date)
even_share(amount, members) / share_of_total(defn, occ, person_id) / splits_are_exact(defn, occ)
```

**Sharing is per occurrence, not per item.** "Has Sam paid me back" is a question about a
specific bill, which is why the paid state lives in `Item.shares_paid[occ]` and why
`all_paid` takes a date.

**An even split divides by members *plus the user*.** The user is one of the people
consuming the subscription, so $12.99 across two housemates and yourself is $4.33 each.
Splitting among members only would double "owed to you". `splits_are_exact` checks an
explicit split *fits inside* the occurrence — an edited split can be made to add up to more
than the bill, and the app should not claim someone owes money nobody was charged.

### 5.7 `tracker` — consumables

```python
tracker_cost_per_use(item) -> float
tracker_total(item) -> float                    # quantity × per-use
tracker_days_elapsed(item, today) -> int | None
tracker_cost_per_day(item, today) -> float | None
tracker_cost_per_year(item, today) -> float | None
tracker_costs(items, today) -> list[TrackerCost]
tracker_total_spend(items) / tracker_annualised_spend(items, today) -> float
```

`per_day` and `per_year` are **`None`** on the day of purchase and for an undated item —
"cost per day" on day zero is not a very large number, it is a question not yet answered.
An unmeasurable rate is not a zero rate, so undated items are omitted from the annualised
total rather than dragging it down.

### 5.8 `detect` — pattern finding

```python
merchant_key(description) -> str
group_by_merchant(transactions, *, expenses_only=True) -> dict[key, list[Transaction]]
is_regular(dates, tolerance=0.35) -> bool
monthly_estimate(amount, interval_days) -> float   # _interval_stats is private
matching_definitions(view, key) -> list[item_id]
detect_subscriptions(view, *, today=None, min_occurrences=2, tolerance=0.35) -> list[UntrackedCharge]
untracked_spend(view, *, today=None) -> UntrackedSpend
recurring_payments(view, merchant=None, *, min_occurrences=2) -> list[RecurringGroup]
upcoming_renewals(view, today, *, days=60, subscriptions_only=False) -> list[Occurrence]
renewal_count(view, today, *, days=60) -> int
days_until_next(item, today) -> int | None
```

`merchant_key` normalises a bank description to a stable identity: digits go (card
references, store numbers, dates), so does a short list of payment-rail vocabulary
(`EFTPOS`, `VISA`, `PURCHASE`, `PTY`, …). "NETFLIX.COM 4482" and "NETFLIX.COM 22/08"
both become `NETFLIX`. An all-noise description yields `""`, and callers skip those rather
than inventing a merchant.

`upcoming_renewals` walks **leaf items**, as every other plan total does: a category node
holding children is a group, and counting it here as well would make the timeline
disagree with the ledger about the same plan.

`is_regular` is the median gap with a coefficient of variation under 35% and a median of at
least 5 days. **Two same-day transactions break regularity**, which is correct — a
"subscription" billed twice on one day is two transactions, not a schedule.

**Detection proposes; it never writes.** Every result is a suggestion the user accepts or
dismisses. There is a test asserting `detect_subscriptions` does not change the store.

### 5.9 `categorise` — rules and suggestions

```python
seed_rules() -> list[Rule]                # 59 shipped defaults
is_seed(rule) -> bool
order_rules(rules) -> list[Rule]
rule_matches(rule, description, merchant=None) -> bool
categorise(description, rules, *, merchant=None) -> Decision
categorise_many(transactions, rules) -> list[(Transaction, Decision)]
uncategorised(transactions) -> list[Transaction]
suggest_rules(view, *, min_occurrences=2, limit=12) -> list[RuleSuggestion]
suggestions_summary(view, *, min_occurrences=2) -> str
recategorise_all(store, *, rules=None, overwrite=True) -> int
accept_suggestion(store, suggestion, *, category=None) -> Rule
ensure_seed_rules(store) -> int
categories_in_use(view) -> list[str]
coverage_by_category(view, start=None, end=None) -> dict[str, float]
```

Two layers, checked in order: the user's rules, then the seeded defaults. The seeded
defaults are **ordinary `Rule` objects** at negative priority — editable and deletable like
any other, so there is no hidden layer that behaves differently from a rule the user wrote.
Negative priority *is* the mechanism that makes a user rule (priority 0 by default) win.

**A keyword shorter than five characters must stand on its own as a word.** "bp" is a fuel
brand, but as a bare substring it is also inside "subprime", and a rule that silently
re-categorises unrelated spending is exactly the quiet wrongness this app exists to avoid.

**`recategorise_all` protects a hand-set category.** A transaction with a category but no
`rule_id` was categorised by hand, and a hand-edit outranks a rule, so re-running skips it
(`protect_manual=True`, the default). A rule the user has just edited still takes effect on
the rows that carry its `rule_id`. Without this, pressing "re-run categorisation" reset
every hand-set category to whatever a rule said, or to nothing.

**The suggestion is the real feature.** Un-categorised transactions are grouped by
merchant key — the same grouping `detect` uses — so "categorise 54 transactions" becomes
"accept these nine rules". Accepting one writes an ordinary rule and re-runs.

### 5.10 `search`

```python
search(view, query, *, limit=40) -> list[Result]
match_score(text, query) -> float | None
kinds_present(results) -> list[str]
```

Matches across item names, categories, tags, notes, transaction descriptions and
categories, people, accounts, goals, consumables, rules and canvas names. Ranking:
exact (1.0), prefix (0.9), word-prefix (0.8), substring (0.6), fuzzy subsequence (0.35);
ties broken by recency then title.

A row's *own* name is scored at full weight; every other field is capped at `SCORE_SUBSTRING`,
so a buried tag can never outrank a title. The cap applies to the weaker fields, not to the
whole row — a transaction whose description prefixes the query keeps its 0.9 even if its
category happens to be an exact match on a weaker field.

### 5.11 `importers` — parse, normalise, identify, reconcile

The pipeline (§19):

```
file ──▶ parse ──▶ normalise ──▶ identify ──▶ store
         csv/ofx   date, signed   external_id
                   amount, desc   or fingerprint
```

```python
# Parsing
parse_file(path, *, source=None) -> ParseResult      # dispatches on content, not just suffix
parse_bytes(data, *, source="upload") -> ParseResult # what a sync response gives you
parse_csv(text, *, source="csv", delimiter=None, column_map=None) -> ParseResult
parse_ofx(text, *, source="ofx") -> ParseResult
detect_columns(rows, headers=()) -> ColumnChoice
find_header_row(rows) / find_first_data_row(rows, start) -> int | None
parse_date(text, *, day_first=True) -> date | None
parse_amount(text) -> float | None
looks_like_amount(text) -> bool
parse_ofx_date(text) -> date | None
infer_date_order(samples, *, default_day_first=True) -> bool
sniff_delimiter(text) -> str

# Identity
fingerprint(when, amount, description, account, source, *, index=0) -> str
plan_import(existing, rows, *, source, account) -> ImportPlan
build_transaction(planned, *, source, account, rules=None, now=None) -> Transaction

# The write path
import_rows(store, rows, *, source, account, rules=None, now=None, skipped=(), warnings=()) -> ImportSummary
import_parse_result(store, parsed, *, account, ...) -> ImportSummary
import_file(store, path, *, account, ...) -> ImportSummary
rolled_up_import_summaries(summaries) -> ImportSummary

# Reconciliation
reconcile(view, *, start, end) -> Reconciliation           # asks
apply_reconciliation(store, result) -> int                 # acts, and bumps version
```

Normalisation produces **one shape** — `RawTxn(date, amount (signed, negative = out),
description, external_id, raw)` — so nothing downstream cares where a row came from.

**Column detection, not a fixed schema.** Headers win where they exist; where they do not,
each column is scored against date and amount predicates over a sample, already-chosen
columns are excluded, and position is the last resort. `_NUMERIC_CELL` is deliberately
strict (a cell that is *entirely* a number) because a permissive test scores any
description with digits in it as an amount column. It must work with preamble junk above a
header and with no header at all.

Where there is no header, the row the data *starts* on is chosen by **which candidate read
the most rows**, earliest winning ties — not by "the first row that looks like data" and
not by parse rate. A summary line such as `Period Start,01/09/2026,Opening Balance,1,234.56`
carries a bare date and bare numbers, so it passes the looks-like-data test and then wins
the amount column for itself, making every real row below it unreadable. Counting rows is
what makes the correction safe: a rate would let a one-row suffix that happens to parse
beat a longer prefix, and a junk row in the middle would then cost the good rows above it.

**The delimiter is chosen by what it splits, not by how often it appears.** Counting
characters picks the wrong one for a semicolon export whose descriptions contain commas —
`2026-09-01;COLES, NORTHLAND, VIC;-12,34` — and because the guess is never retried, a
whole valid statement then imports as *zero* rows. Each candidate is instead scored on the
rows it produces: consistency (how many rows share a width) first, then field count.

**Ambiguous D/M vs M/D is decided from the whole column**, not per row
(`infer_date_order`). A statement where one row reads as April and the next as March is
worse than either convention applied consistently; with no evidence it defaults to
day-first.

**An unreadable row is counted and reported**, never dropped. `ParseResult.describe()`
gives "412 read, 7 skipped", and `skipped_samples` names the first few with reasons.

#### 5.11.1 Identity and dedupe — the part that matters most

Keying identity on `date + rounded amount + lowered description` means two genuine
same-day, same-amount, same-merchant transactions — two $4.50 coffees — collide and the
second is **silently dropped**. For a tool whose entire job is reconciliation, silently
dropping a transaction is the worst available failure mode.

So identity is, in order:

1. **`external_id`** — the aggregator's stable id, unique per account. Preferred always,
   and checked *within one batch* as well as against the store, because an id appearing
   twice in one file is the aggregator repeating itself rather than two payments.
2. **`fingerprint`** — the hash above, used *only* for rows with no external id, and
   compared *only* against rows from the same source. It includes a **disambiguator
   index**: within one batch, rows sharing date/amount/description are numbered in file
   order, so both coffees survive (indices 0 and 1) and re-importing the same file
   reproduces the same numbering, so both are still recognised as already present.

Imports report both counts — how many were new, how many were skipped as duplicates. A
skip count that looks wrong is a signal, not noise.

**Reconciliation** needs the same merchant *and* an amount within 2 cents, on a date within
a 10-day window. Asking and acting are two functions rather than one flag: `reconcile`
takes a `StoreView` and answers, `apply_reconciliation` takes the `Store` and writes — so
the write goes through the path that bumps `store.version`. Two transactions cannot both
claim one occurrence.

### 5.12 `sync` — SimpleFIN

```python
claim(token, *, transport=None) -> str                 # setup token → Access URL
claim_and_store(secrets, token, *, transport=None, path=None) -> str
revoke(secrets, *, path=None) -> None
fetch_accounts(access_url, *, since=None, include_pending=False, transport=None) -> FetchResult
sync(store, *, access_url, today, imported_at=None, lookback_days=90,
     transport=None, rules=None, include_pending=False, since=None) -> SyncResult
run_sync(store, secrets, *, today, imported_at=None, transport=None, rules=None) -> SyncResult
sync_enabled(view, secrets=None) -> bool
```

Stdlib `urllib` only, no new dependency. The `Transport` protocol is the one seam tests
stub: **nothing above it is protocol logic and nothing below it is.**

- The **setup token** is base64 of a claim URL; `decode_token` refuses anything that does
  not decode to a `http(s)` URL rather than guessing.
- A `403` on claim means the token was already claimed — one clear message, no retry.
- The **Access URL is the credential** (it embeds Basic Auth). `split_credentials` lifts
  the credentials into an `Authorization` header and strips them from the request URL.
- **SimpleFIN returns no category**, only a description. That is why §5.9 is the
  load-bearing part of this feature rather than a nicety.
- **`today` is required, not defaulted to the clock.** It decides the lookback window,
  so a caller who forgot it would reach for real time and make the run unreproducible.
- **Incremental** by a per-account high-water mark (`Account.high_water`), pulled back by
  `OVERLAP_DAYS = 5` so a late-posted transaction is not missed. The overlap is re-fetched
  and deduped, which is cheaper than a gap.
- **Only matching-currency accounts are imported.** A non-matching one is recorded and its
  balance kept so it is visible, but not one transaction from it is imported and it is
  never summed into net worth.
- **Pending transactions are skipped by default**: a pending row's id changes when it
  posts, so importing it now would duplicate it later.
- **A row whose posted date cannot be read is counted and warned about**, never filtered
  away silently. A transaction that disappears into "no new transactions" is exactly what
  §22 forbids.

**Failure states are explicit and this is the point of the module.** `SyncResult.ok`,
`.error`, `.needs_reconnect`. A revoked connection (403), a lapsed bridge subscription
(402) and an expired consent all surface as "reconnect required". `describe()` on a failure
says "Sync failed: …" and can never be mistaken for "nothing new". There is a test that
asserts a failed sync and an empty sync produce different results — a failed sync that
looks like "no new transactions" is the failure mode that makes a user stop trusting the
app.

---

## 6. The hooks layer

Between the engine and the UI, and Qt-free like everything below it.

### 6.1 The contract

```python
@dataclass(frozen=True)
class HookContext:
    store: StoreView          # read-only facade; no mutation API exists on it
    lens: str                 # "week" | "month" | "quarter" | "year"
    start: date
    end: date
    label: str                # "October 2026"
    today: date               # injected, never read from the clock
    peers: Mapping[str, WidgetData]   # sibling widgets on the same canvas

def hook(ctx: HookContext, config: dict) -> WidgetData | None
```

```python
@dataclass(frozen=True)
class WidgetData:
    value: float | None
    delta: float | None
    series: list[tuple[date, float]] | None
    rows: list[Row] | None
    sign: int | None          # -1 / 0 / +1, for colour pairing
    note: str | None
    extra: dict[str, Any]     # widget-specific extras; never a place to hide the main number

@dataclass(frozen=True)
class Row:
    label: str
    value: float | None
    detail: str
    sign: int | None
    tags: tuple[str, ...]
    target: dict[str, Any]      # navigation hint
    reference: dict[str, Any]   # enough to act on the row: {"kind", "id", "date", ...}
```

Rules:

| Rule | Why |
|---|---|
| **Read-only** | A hook reads the store; it never writes. `StoreView` has no mutation method. |
| **Pure** | Same `ctx`, same output. What makes output cacheable and shots reproducible. |
| **No runtime loading** | Hooks are supplied in code at build time. No plugins directory, no `importlib`. |
| **Band-aware, not band-dependent** | A hook may return only what its band shows, but never different *values* per band. |
| **Absence is `None`** | A hook with no data returns `None`; the widget hides and the grid reflows. Never a zero that means "unknown". |

`WidgetData.is_empty` is `value is None and not series and rows is None and not note and
not extra`. Note the asymmetry: an explicitly empty `rows=[]` is **not** empty — it is a
widget with a considered empty state to render (the budget report with no budgets).

`effective_sign` is derived from the value unless set outright — for a figure where up is
bad, set `sign=-1`.

### 6.2 Templates (§28) — derived metrics without a second widget class

```python
ratio(a, b) -> Hook                 # a over b, guarded against a zero denominator
share_of(a, b) -> Hook              # a as a percentage of b
delta(a, b, *, label=None) -> Hook  # a minus b
cumulative(a) -> Hook               # running total across the period
rolling_mean(a, n) -> Hook          # n-period mean, growing in from the left
scale(a, factor) -> Hook
combine(*hooks, op="sum", label=None) -> Hook
as_hook(source) -> Hook             # a callable, or a literal as a constant hook
```

Each takes hooks or plain numbers and returns a hook, so a derived widget is an ordinary
catalogue widget handed a different hook — **not a new widget class**. Series are paired
**on dates, not on position**: two widgets built from different lenses can have different
lengths, and zipping them positionally would silently compare October to August.

There is deliberately no expression parser and no formula box. Anything outside this set is
still just a Python function.

### 6.3 The cache

```python
cache_key(hook_id, config, ctx) -> tuple
HookCache(maxsize=512).call(hook_id, hook, ctx, config) -> WidgetData | None
```

The key is `(hook_id, config, lens, start, end, label, today, store.version, currency,
peers_key)`. Two additions to the spec's stated key are load-bearing:

- **`store.version`** makes invalidation exact rather than timed. There is no TTL.
- **`peers_key`** — a hook that reads its siblings depends on them, so they must be in the
  key or the second of two identical recomputes returns the first one's answer.

A `None` result is cached too: absence is a result, and asking again should not recompute.

### 6.4 The 35 hooks

`hooks/library.py` holds one function per catalogue entry, registered by id via
`@hook("id")`. `get_hook(id)` and `hook_ids()` are the lookup. Each is a thin reading of
the engine — if a hook needed arithmetic of its own, the arithmetic would belong in the
engine where it can be tested without a display.

---

## 7. Invariants

If a change breaks one of these, the change is wrong.

1. No module under `store/`, `engine/` or `hooks/` imports Qt. (`tests/test_layering.py`)
2. No engine function reads the clock; `today` is a parameter, and where it is needed it
   is *required* rather than defaulted. (`importers`/`sync` stamp `imported_at`, which is a
   timestamp, not a decision input — that one `datetime.now()` is deliberate.)
3. A query never mutates the store. A hook never mutates the store.
4. No engine function raises on degenerate input.
5. A monthly item on the 31st fires exactly once in every month, clamped, never skipped,
   never rolled over.
6. An override on one occurrence changes that occurrence and no other.
7. Two genuine same-day same-amount same-merchant transactions both survive an import.
8. Re-importing the same file imports nothing.
9. A failed sync can never be mistaken for "no new transactions".
10. A cancelled occurrence is excluded from every total and included in every row list
    that asks for it.
11. `Store.version` increases on every write, and the hook cache is keyed on it.
12. A save is one transaction and **rolls back on failure** — a half-done save must not
    be completed by the next commit — and a store with no directory refuses to write
    rather than choosing a location for itself.
13. A store that cannot be read is preserved, never destroyed and never silently
    overwritten, and the reason reaches the user. A database has to be *moved* aside to be
    preserved, where a JSON file could be left in place.
14. The active canvas is always one that exists; there is always at least one canvas.
15. Money is rounded only at display.
16. Reading never writes. A read-only load creates no directory, copies nothing aside,
    and refuses every write; `--shot` renders through it. (`tests/test_store.py`,
    `tests/test_ui_hardening.py`)
17. A restore can itself be undone: `restore()` snapshots the present before it replaces
    it, and retention never prunes a deliberate snapshot. (`tests/test_history.py`)
19. A store another process holds is **busy, not corrupt**: it is never moved aside, and
    no substitute store is opened in its place. A second window's stale save is refused
    rather than silently overwriting the first. (`tests/test_review_fixes.py`)
18. `monthly_equiv` is a property of the rule, not of the range being asked about, so the
    rollup and the scheduler cannot disagree about the same plan.

---

## 8. Recipes

**Add a widget.** Add a `WidgetSpec` to `_CATALOGUE` in `ui/widgets/registry.py` (id, name,
group, hook id, bands, default size, floor, summary), write a `@hook("id")` function in
`hooks/library.py`, and a `@renderer("id")` function in `ui/widgets/render.py`. A test
asserts every spec has both a hook and a renderer, and that every spec's default size is at
or above its floor.

**Add a parser.** Write `parse_x(text, *, source) -> ParseResult` producing `RawTxn`s, then
add the suffix to the dispatch in `parse_file`. CAMT.053 is the known candidate — the spec
marks it not required and the dispatch is the single entry point it would plug into.
Nothing downstream needs changing: normalisation is one shape.

**Add a recurrence part.** Handle it in `engine/rrule.py` — as a field on `RRule`, a branch
in `_expand()`/`_passes_day_filters()`, a branch in `rate_per_month()`, and a phrase in
`_describe()`. Add a preset to `build_recurrence` in `ui/editors.py` only if it deserves one:
anything else is already reachable through the editor's custom field, which is the point of
having adopted a standard syntax rather than a menu. A part that is understood but unusable
goes in `IGNORABLE`, which surfaces it to the editor instead of dropping it silently.

**Change the schema.** Bump `SCHEMA_VERSION`, add `vN_to_vN1(doc) -> dict` to `MIGRATIONS`
in `store/migrations.py`. Give every new field a default so an older document still loads.
The backup-before-migrate step is automatic.

**Replace the frontend.** Delete `ui/`, keep everything else. You will need: `paths`,
`store` (load/save/mutate), `engine` (all numbers), `hooks` (the widget data contract), and
`demo.demo_store()` if you want a screenshot harness. `tests/` will need the `ui`-specific
files removed; the ~500 non-UI tests should pass unchanged, which is the check that the
backend really is decoupled.

---

## 9. Where the spec is silent, and what was decided

Recorded because these are judgement calls, not transcription.

| Question | Decision | Why |
|---|---|---|
| How do plan items join to transaction categories? | Added `Item.category` | The spec gives transactions and rules a category but not items, so budget-vs-actual had no join key. Without it the comparison has nothing to group by. |
| How is identity stored for dedupe? | Added `Transaction.fingerprint` | §21 requires a fingerprint fallback; nothing in the field table holds it. |
| Where does a sync high-water mark live? | Added `Account.high_water` | §22 requires it persisted per account. |
| Is a shared split divided by members or by members + user? | Members **+ the user** | The user is one of the people consuming the subscription. Splitting among members only would double "owed to you". |
| What is a "budget" for budget-vs-actual? | A *named* category | An uncategorised plan line is not a budget; uncategorised *spend* still gets its own row. |
| Where does the ledger's headline total come from? | The engine's plan total, not the sum of rows | Rows include cancelled occurrences for the user to see; the money must exclude them. |
| Which bands do the "Tall/Full" widgets render at? | Added width bands to five list widgets and three full-width charts | §27 requires a widget to render acceptably at its floor. 27 of 35 declarations match §29's table exactly; the 8 differences only *add* bands, none remove one. The alternative reading — raise those floors to 8 columns so they never render narrower — would change the seeded canvas. |
| How are derived metrics exposed? | `ratio`, `share_of`, `delta`, `cumulative`, `rolling_mean`, `scale`, `combine` | The spec names five templates and says the set is not closed. Two were added because they are the same kind of shorthand. |
| What happens to unreadable import rows? | Counted, with reasons, in `ParseResult.skipped`; a synced row with no readable date gets a warning and a count | §32 requires "412 read, 7 skipped"; §22 forbids a dropped transaction that looks like a successful sync. |
| Does re-running categorisation overwrite a hand-set category? | No | A hand-edit outranks a rule. `protect_manual=False` is there for the rare caller who wants the rules to win. |
| Does an unlinked goal share the surplus minus the linked commitments? | No — the whole surplus | The linked savings transfer is already an expense in the surplus. Subtracting it again lost it. |
| Asking whether a transaction paid an occurrence, vs marking it | Two functions: `reconcile` (view) and `apply_reconciliation` (store) | Marking through a read-only facade both breaks §15.2 and skips the version bump the hook cache depends on. |

### Known gaps

- **Tab from a ledger row straight into its amount** (§31) is not implemented. Tab moves
  between fields within a popout; it does not jump from a row to its editor.
- **Skipped-row line numbers** in an import summary are relative to the file with blank
  lines removed, so they can be a few lines off in a file that has them. Reporting-only.
- **Net-worth snapshots** are read but never captured automatically — the spec leaves the
  schedule undecided (§38). `net_worth_series` reports whatever readings exist.
- **CAMT.053** is not parsed; see §8.
- **IBM Plex Sans is not bundled.** `ui/app.py` looks in `assets/fonts/` for TTF/OTF and
  otherwise resolves the first available system family from a candidate list. Qt cannot
  load the design system's woff2. Bundling the OFL-licensed TTF is the open step.
