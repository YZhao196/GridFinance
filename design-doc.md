# Budgeting Tool — design document

Written 2026-10-07. Supersedes `docs/app-proposal.md`. Everything the app needs,
specified as if nothing existed: the product case, the data model, the engine, the import
and sync paths, the UI layer, and the order to build them in.

**Status, 2026-10-08.** Parts II to V are built and tested — 867 tests, no Qt. **The UI
layer (Part VI, and §30 in particular) describes a frontend that has since been removed to
be redesigned.** Read those sections as requirements for the next frontend rather than as a
description of what exists. `BACKEND.md` is the contract that frontend reads.

Companion docs: [PRODUCT.md](../PRODUCT.md) (brand and personality),
[buildnexus-mapping.md](buildnexus-mapping.md) (design-system tokens),
[sync-and-autocat-gameplan.md](sync-and-autocat-gameplan.md) (sync and categorisation detail).

---

# Part I — The product

## 1. The idea

Most budgeting tools make you pick a side. Either you get a **forecast** — a plan you
trust but never check against reality — or a **reconciled report** — what actually
happened, with no sense of what was supposed to. Whichever way you pick, the other becomes
an afterthought, and the one question that matters is answered by whichever view you
happened to open.

This app keeps both live at once:

- **The plan** — a recurring ledger of income, expenses, subscriptions and shared costs,
  projecting forward: what *should* happen this month, this quarter, this year.
- **The reality** — imported bank transactions, reconciled against that plan: what
  *actually* happened.

Neither is the advanced mode of the other. They are peers, held to the same standard, and
the app's job is to survive being read side by side.

**Positioning.** The plan and the bank statement live in the same tool, at the same
standard.

## 2. Who it's for

One person managing their own finances — recurring bills, a few subscriptions shared with
housemates or family, savings goals, and a bank account to reconcile against.

A **desk tool, not a lifestyle app**. Precise and information-dense rather than simplified,
closer to a terminal than to a consumer finance app. Used deliberately and regularly, at a
desk, by someone who wants the real number rather than a comfortable one.

## 3. What it does

| Capability | What it covers |
|---|---|
| **Recurring ledger** | Income and expense items with recurrence rules (weekly, monthly, custom intervals, day-of-month), start and end dates, due dates, priorities, nesting. |
| **Occurrence-level editing** | Change or cancel a single occurrence of a recurring item without rewriting the ones already past. |
| **Bank import** | CSV and OFX statements, with column detection for headerless exports. |
| **Bank sync** | Live transactions via SimpleFIN through an Australian CDR bridge. Opt-in. |
| **Reconciliation** | Dedupe on stable external ids; auto-categorise against user rules; review unmatched. |
| **Shared plans** | Split subscriptions and costs with named people; per-occurrence shares; settle-up. |
| **Savings goals** | Target-based goals linked to expenses, with a projected completion date. |
| **Analytics** | Budget-vs-actual, spending composition, cash flow, net worth history, liquid-balance forecast, month-over-month. |
| **Detection** | Recurring bank charges absent from the ledger; renewal timelines. |
| **Derived metrics** | Any widget can be handed a custom computation (`income / expenses`) through its data hook (§21). |

## 4. Principles

These decide arguments. Carried from [PRODUCT.md](../PRODUCT.md):

| Principle | Means |
|---|---|
| **Numbers lead** | Every screen's first job is showing a number or a trend, not framing one. |
| **Plan and reality are peers** | The ledger and the reconciled view get equal weight and equal polish. |
| **Density is a feature** | A power-user tool earns the right to show more at once, provided it stays scannable. |
| **Functional colour only** | Green/red/amber always encode a real state, always paired with a label or position. |
| **Calm under bad news** | Deficits and overruns are reported plainly, in the same visual language as good news. |

## 5. Non-goals

Declared so they stop being re-proposed. Each costs effort and buys nothing for one user
at one desk.

- **Multi-user, family, or shared accounts.** Shared *costs* are in scope; shared *logins* are not.
- **Mobile, web, or sync between devices.** One desktop, one store.
- **Multi-currency.** One currency per install; a non-matching account is flagged, never summed.
- **AI assistance, chat, or natural-language entry.**
- **Investments and holdings tracking.** Separate effort with its own plan; only worth doing if investments are actually held.
- **A plugin or scripting surface.** See §28 — extension is by code, deliberately.
- **An expression parser or formula box.** See §28.
- **Distribution.** No installer, no auto-update, no telemetry, no crash reporting, no
  packaging step. This is a **personal program**: one user, one machine, one store, run
  from source. Anything whose only purpose is shipping software to other people is out.

---

# Part II — Architecture

## 6. Layers

Four layers, one direction of dependency:

```
        ┌─────────────────────────────────────────────┐
   UI   │  shell · canvas · tiles · widgets · charts  │
        └────────────────────┬────────────────────────┘
                             │  reads through, never computes
        ┌────────────────────▼────────────────────────┐
 Hooks  │  HookContext · WidgetData · hook templates  │
        └────────────────────┬────────────────────────┘
                             │
        ┌────────────────────▼────────────────────────┐
Engine  │  recurrence · periods · ledger · analytics  │
        │  goals · shared · detection · categorise    │
        │  importers · sync · search                  │
        └────────────────────┬────────────────────────┘
                             │
        ┌────────────────────▼────────────────────────┐
 Store  │  entities · load/save · migration · secrets │
        └─────────────────────────────────────────────┘
```

**The dependency rule.** The store and the engine import **no Qt, at all**. Every number
the app shows is produced by a plain-Python function that takes data and returns data. The
UI's job is layout, painting and input; it is never the place where a figure is calculated.

This is not a style preference. It is what makes the engine unit-testable without a display,
what makes `--shot` screenshots deterministic, and what stops "the ledger says X but the
chart says Y" from being possible.

**Corollaries.**

- A UI file may not `import` from another UI file's internals; shared behaviour goes down a layer.
- The UI reads through the engine's public functions, not through raw store structures.
- No engine function takes a widget, a QObject, or a signal.

## 7. Process and threading

Single process, single UI thread, with a small worker pool for two jobs only:

| Job | Why off-thread |
|---|---|
| Bank sync (HTTP) | Network latency; a hung request must not freeze the window. |
| Import of a large statement | Parsing thousands of rows is noticeable. |

Both report back to the UI thread through a queued signal carrying a plain result object.
Nothing else is threaded — everything else is arithmetic over local data, and correctness
beats parallelism there.

## 8. Where data lives

One directory, `data/`, beside the app, gitignored, created on first run:

```
data/
  store.db            # the whole domain: items, accounts, goals, tracker, people,
                      # transactions, rules, and preferences — SQLite
  secrets.json        # sync credentials only — never logged, never exported
  backups/            # timestamped snapshots, written before any migration
```

One SQLite database rather than a file per entity: the app's queries span entities (a
budget-vs-actual view reads items *and* transactions), and one database makes those reads
atomic and cheap. Preferences live in a row inside it rather than in a file of their own, so
there is one path rather than two and no question about which is live.

The schema is **derived from the entities' own type hints**, so adding a field stays a
one-line change. A scalar worth sorting or filtering on is a real column; a list or dict —
the per-occurrence bags, which are keyed by a date and never queried alone — is a JSON
column.

Writes are **transactional**: a save commits or does not happen, so a crash part way through
leaves the previous state intact rather than a half-written file. A second window cannot
overwrite the first: a save is refused if another process has written since this one loaded.

**Built, "beside the app" means beside the executable.** `finance_tool.spec` builds a
single windowed `Finance Tool.exe` (`python -m PyInstaller finance_tool.spec --noconfirm`),
and `paths.py` asks two separate questions — where to *write*, which is the executable's own
folder, and where bundled read-only files are, which is the temporary directory a one-file
build unpacks into. A one-file build deletes that directory on exit; deriving the store's
location from `__file__` would therefore delete the user's data every time they closed the
window. The two answers are kept apart for that reason and a test asserts they stay apart.

---

# Part III — Data model

## 9. The definition/occurrence split

The central modelling decision, and the one everything else leans on.

A **definition** is a plan item: "Rent, $520, monthly, from 2026-01-01, open-ended".
An **occurrence** is that definition landing on a specific date: "Rent, 2026-11-01".

```
Definition (id="d_rent")
  ├── occurrence 2026-09-01   paid
  ├── occurrence 2026-10-01   paid
  ├── occurrence 2026-11-01   ← amount overridden to 545 (a rent rise)
  └── occurrence 2026-12-01
```

Occurrences are **never stored as rows**. They are derived on demand from the definition
plus the requested date range. The only per-occurrence state that *is* stored is the
exception:

| Stored per definition | Meaning |
|---|---|
| `overrides: {occ_iso: {field: value}}` | This occurrence differs — amount, note, due date. |
| `cancelled: [occ_iso, …]` | This occurrence doesn't happen. |
| `paid: [occ_iso, …]` | This occurrence has been settled. |

This is what makes "change next month's rent, leave the rest alone" a one-key edit rather
than a rewrite of history, and it's why the ledger can show a plan forward through years
without storing years of rows.

**Resolution.** `occurrence_dates(defn, start, end) -> [date]` gives the dates;
`effective(defn, date) -> Occurrence` applies the overrides and the definition's defaults.
Every consumer — ledger, budgets, goals, charts — goes through these two functions. There
is no third way to ask what happens in a month.

## 10. Entities

### 10.1 Item definition

| Field | Type | Notes |
|---|---|---|
| `id` | str | Stable, generated, never reused. |
| `parent_id` | str \| null | Nesting: a category holding items, an item holding line items. |
| `name` | str | |
| `type` | `"income"` \| `"expense"` | Drives sign and column. |
| `amount` | float | Positive; sign comes from `type`, never stored negative. |
| `start`, `end` | ISO date, `end` nullable | `end = null` means open-ended. |
| `recurrence` | object \| null | `null` = one-off. See §11. |
| `due` | str \| null | Day-of-month or a date; feeds due-soon/overdue. |
| `priority` | int | Ordering and emphasis within a period. |
| `tags` | [str] | |
| `note` | str | |
| `subscription` | bool | Included in subscription rollups and the renewal timeline. |
| `shared` | object \| null | Members and split rule. See §16. |
| `overrides` / `cancelled` / `paid` | maps | §9. |
| `ideal` | float \| null | Target amount, where it differs from actual. |
| `cancel_by` | ISO date \| null | Planned cancellation date. |
| `expanded` | bool | UI-only: whether the row's children are shown. |
| `created_at`, `updated_at` | ISO timestamp | |

**Nesting.** Two levels in practice: a category node with children (a bill with line
items), and a plan item with sub-items. Depth is unbounded in the walk, two in the UI.

### 10.2 The rest

| Entity | Fields | Purpose |
|---|---|---|
| **Account** | `id, name, kind, balance, currency, external_id` | Net worth, liquid balance, sync target. |
| **Transaction** | `id, date, amount, description, account, category, tags, external_id, source, imported_at` | A reconciled or imported bank line. Negative = money out. |
| **Rule** | `id, match, category, tags, priority` | Categorisation. See §24. |
| **Goal** | `id, name, target, saved, linked_def_ids` | Savings target with a projection. |
| **TrackerItem** | `id, name, quantity, per_use_amount, purchased_on` | Consumables; cost-per-use. |
| **Person** | `id, name, email, phone` | Shared-plan counterparties. |
| **Canvas** | `id, name, widgets: [PlacedWidget]` | A user-created screen. |
| **PlacedWidget** | `widget, x, y, w, h, config` | Position on a canvas (§26). |

**Money is a float.** For a single-user personal tool, double precision is correct to
within a cent at any realistic scale, and the alternative — integer cents — would have to
be threaded through every analytic. Totals are rounded only at display.

**Amounts are stored unsigned**, with direction carried by `type` on items and by sign on
transactions. A single `signed(node)` helper is the only place the two conventions meet.

## 11. Recurrence

A schedule is an **RFC 5545 recurrence rule** — `FREQ=MONTHLY;BYDAY=2TU` — stored as text
under `recurrence: {"rrule": "…"}`. A definition with no rule is a one-off. The user is not
limited to a menu of shapes: anything a rule can say, they can use, including the second
Tuesday, the last Friday, every second week on a Monday and a Thursday, and a count or an
end date.

Supported: `FREQ`, `INTERVAL`, `BYDAY` (including ordinals such as `2TU` and `-1FR`),
`BYMONTHDAY` (including negatives, which count back from the end), `BYMONTH`, `BYSETPOS`,
`COUNT`, `UNTIL`, `WKST`. Time-of-day and week-of-year parts are read and reported as
unusable rather than silently discarded, and the editor refuses to save a rule it cannot
parse — a query must never fail, but a *typing mistake* must never become a silent one-off.

**Month-end clamping — the one deliberate deviation from the standard.** RFC 5545 gives
`BYMONTHDAY=31` no occurrence in February. Here it lands on the last day of the month, so a
monthly item on the 31st fires **twelve times a year**: the 31st in March, the 30th in
April, the 28th in February. Never skips, never rolls into the next month. This is a named
rule because it is the single most common source of "the rent is missing" bugs, and it is
worth saying plainly that no calendar library does this by default.

The same clamping applies to a weekday ordinal past the end of a month — `5FR` in a
four-Friday month is that month's last Friday — and to a day used as a filter, so "the 30th"
means 28 February everywhere in this app rather than nowhere.

**Monthly equivalence** is derived from the rule rather than counted over a window. A window
would give a weekly rule 52 or 53 occurrences depending on where it opened, so the rollup and
the scheduler could disagree about the same plan. Clamping is what makes this exact: a rule
on the 31st fires twelve times a year *because* it clamps.

**Ordering.** Occurrences within a period are ordered by due date, then priority, then
name — a total order, so lists never shuffle between renders.

## 12. Migration and versioning

The database carries its version in SQLite's own `user_version`. On load:

1. If the version is lower than this build's, snapshot the store to `data/backups/` first,
   then migrate.
2. Migrations are a list of pure functions applied in sequence.
3. A migration that fails leaves the original file untouched and surfaces one error in
   Settings — never a silent partial write, never a blank app.
4. If the version is *higher* than this build's, the store is refused and left alone. The
   version is not rewritten, so a store written by a newer build stays readable by it.

The version is stamped once, when the database is created — never on every open, which
would relabel a store from the future as one from the present.

An unreadable file is **moved** aside to `data/backups/` rather than copied: unlike a bad
JSON file, one left in place would fail every later open, so there would be nothing left to
recover from either. A refused migration is recoverable by restoring the backup; that is the
whole point of step 1.

**Revision history.** `data/backups/` is not just a migration safety net. A snapshot is
taken automatically as you work — throttled to one every ten minutes, so it is a history of
states rather than a log of keystrokes — and deliberately before anything the app does in
bulk. Automatic snapshots are thinned by age: all of today, one an hour for a week, one a
day for a year. A snapshot you asked for is never pruned.

The History page lists them, restores one, and exports one. Restoring is **safe**: the
present is snapshotted before an older state replaces it, so a restore is itself something
you can undo from the same page. That matters more than any diff would — the question a
finance app's history has to answer is "was Rent $520 or $545 when I set this up", and the
honest way to answer it is to look, with a way back.

## 13. Secrets

`data/secrets.json` holds the sync credential and nothing else. Rules:

- Never logged, never included in an export or a bug report.
- The sync credential is stored as entered and shown only behind a password-echo field.
- Revoking is a delete: a removed credential leaves no residue in the store.

The bank password is never stored, and never entered into this app — the CDR consent flow
happens on the bank's own page (§23).

---

# Part IV — The engine

## 14. Module map

One module per concern, each independently testable, none importing Qt:

| Module | Responsibility | Representative API |
|---|---|---|
| `recurrence` | Occurrence generation from definitions | `occurrence_dates`, `next_occurrence`, `monthly_equiv`, `fires_in` |
| `periods` | Lenses and date ranges | `range_for_lens`, `step_lens`, `week_ranges`, `iso_week_range` |
| `ledger` | Totals, summaries, budgets | `occurrence_dates`-based `totals_in_range`, `plan_summary`, `budget_vs_actual`, `bills_in_range` |
| `analytics` | History and projection | `history`, `period_summary`, `forecast`, `forecast_band`, `net_worth_series`, `cashflow_links` |
| `goals` | Goal projection | `goal_eta`, `savings_rate` |
| `shared` | Splits and settle-up | `member_shares`, `all_paid`, `who_owes`, `people_roster` |
| `tracker` | Consumables | `tracker_cost_per_use`, `tracker_cost_per_day`, `tracker_cost_per_year` |
| `detect` | Pattern finding | `merchant_key`, `detect_subscriptions`, `upcoming_renewals`, `recurring_payments` |
| `categorise` | Rules and suggestion | `categorise`, `suggest_rules`, `recategorise_all` |
| `search` | Global search | `search(store, query) -> [Result]` |
| `importers` | Statement parsing | `parse_file`, `parse_csv`, `parse_ofx` |
| `sync` | Bank sync | `claim`, `fetch_accounts`, `sync` |

## 15. Contracts

Four rules the engine holds itself to, because the UI depends on all four:

1. **Pure.** A function of its arguments and the store snapshot. No clocks, no globals, no
   I/O except in `importers` and `sync`. "Today" is always passed in as a parameter — this
   is what makes the whole engine testable and screenshots reproducible.
2. **Read-only over the store**, except for the explicit mutation API (§10.1's per-occurrence
   setters). A query never mutates.
3. **No Qt.** Enforced by a test that imports every engine module in a process without
   PyQt available.
4. **Total.** Degenerate input returns an empty or zero result, never raises: no
   transactions, a zero denominator, a goal with `target = 0`, a range with no occurrences.

## 16. Shared plans

The subtle one, because it is per-occurrence rather than per-item.

A shared item declares members and a split:

```
shared = {members: [person_id, …], split: "even" | {person_id: amount}}
```

For any occurrence, `member_shares(defn, occ_date)` returns each member's owed amount, and
`all_paid(defn, occ_date)` says whether everyone has settled *that* occurrence. Paid state
is stored per person per occurrence.

This matters because "has Sam paid me back" is a question about a specific bill, not about
a subscription in general — and answering it in aggregate is how you lose track.

## 17. Detection

`merchant_key(description)` normalises a bank description to a stable merchant identity by
stripping card references, dates, and store numbers. Everything detection does groups by
that key:

- **Untracked charges** — merchant keys appearing on ≥2 occasions at a regular interval,
  with no matching definition in the ledger. Surfaced as "this looks like a subscription
  you haven't tracked", never auto-created.
- **Renewal timeline** — the next *n* days of due dates across all items.
- **Recurring payments** — grouped historical occurrences for a merchant.

Detection proposes; it never writes. Every detection result is a suggestion the user
accepts or dismisses.

## 18. Search

One function, one index. `search(store, query)` matches across item names, notes, tags,
transaction descriptions, people and account names, returning typed results with enough
context to render a result row and navigate to it. Ranking: exact prefix, then word prefix,
then substring, then fuzzy; ties broken by recency. Results carry their target, so the
palette can jump straight there.

---

# Part V — Import, sync and categorisation

## 19. The import pipeline

```
file/sync ──▶ parse ──▶ normalise ──▶ identify ──▶ categorise ──▶ review ──▶ store
                │            │             │              │
            csv/ofx      date, amount   external_id    rules engine
                         + sign         or fingerprint  → category + tags
```

Each stage is a separate pure function, which is what makes the pipeline testable
end-to-end from a fixture file with no bank and no network.

## 20. Parsers

| Format | Notes |
|---|---|
| **CSV** | **Column detection**, not a fixed schema. Score each column against date/amount/description predicates over a sample, exclude already-chosen columns, fall back to position. Must work with no header row and with preamble junk above the header. |
| **OFX** | Tag extraction; the same normalised output as CSV. |
| **CAMT.053** | ISO 20022 XML statement. Not required, but the parse dispatch is a single entry point and this is the one format ANZ can produce on request — an afternoon's work if it ever arrives. |

Normalisation produces one shape: `{date: ISO, amount: float, description: str}` with
**negative = money out**, matching the sign convention of transaction records.

Parsers never guess silently: an unparseable row is counted and reported, so the import
summary can say "412 read, 7 skipped" instead of quietly losing rows.

## 21. Identity and dedupe

**The bug this design exists to avoid.** Keying identity on
`date + rounded amount + lowercase description + account` means two genuine same-day,
same-amount, same-merchant transactions — two $4.50 coffees — collide, and the second is
silently dropped. For a tool whose entire job is reconciliation, silently dropping a
transaction is the worst failure mode available.

**The rule.** Identity is, in order:

1. **`external_id`** — the aggregator's stable transaction id, unique per account. Preferred always.
2. **Fingerprint** — the hash above, used *only* for rows that have no external id (CSV/OFX
   imports), and only against rows from the same source.

Imports report both counts: how many were new, and how many were skipped as duplicates.
A skip count that looks wrong is a signal, not noise.

## 22. Bank sync — SimpleFIN

**Why SimpleFIN and not Basiq.** Live Australian bank data is only available through the
Consumer Data Right, which requires an Accredited Data Recipient — something a personal
desktop app cannot be. Both Basiq and SimpleFIN sit behind that; SimpleFIN reaches it
through an Australian bridge (Redbark) with a fraction of the process: *paste a token*
instead of *create a developer account, register an app, exchange API keys*, one endpoint
instead of four, and no version header.

**The trade.** SimpleFIN returns **no category** — only a description. Categorisation is
therefore entirely this app's own problem, which makes §24 the load-bearing part of the
whole sync feature rather than a nicety.

**Shape.** Three entry points, stdlib `urllib` only, no new dependency:

| Function | Does |
|---|---|
| `claim(token) -> access_url` | Base64-decode the pasted token, `POST` it, get back an Access URL. A `403` means the token was already claimed → clear message, no retry. |
| `fetch_accounts(access_url, since) -> [Account]` | Walk accounts and their transactions; map epoch → ISO date, keep the app's sign convention. |
| `sync(access_url, since) -> ImportSummary` | Feed the transactions through the import pipeline (§19). |

**The Access URL is the credential** — it embeds Basic Auth. It gets the same treatment as
any secret (§13).

**Incremental.** A per-account high-water mark (the last `posted` epoch seen) is persisted
and passed as the start date, so each sync pulls the delta instead of re-fetching and
re-deduping everything.

**Failure states are explicit.** A `403` means the connection was revoked; a `402` means
the bridge subscription lapsed; consents also expire on their own schedule. All three
surface as "reconnect required" in Settings. **A failed sync must never look like "no new
transactions"** — that is the failure mode that makes a user stop trusting the app.

**Off by default.** Sync is behind `settings["enable_bank_sync"]`, default off, so file
import remains the always-on path and the app keeps its offline posture.

**Gated on scope.** Only accounts whose currency matches the app's are imported; a
non-matching account is flagged, never summed into net worth.

## 23. Reconciliation

- Auto-categorise every imported transaction through §24.
- Match transactions against plan occurrences by merchant and amount to mark items paid.
- Anything unmatched lands in **Uncategorised**, with a count surfaced on the canvas — never
  quietly dropped from a budget-vs-actual total.
- Transaction review is a **table**, accepting or rejecting per row, because reviewing a
  whole import is a bulk task (§27's popout rule explicitly carves this out).

## 24. Categorisation

The engine has two layers, checked in order:

1. **User rules** — `match` substring (or merchant key) → category and tags. Explicit, editable, wins.
2. **Seeded defaults** — common keywords ("coffee", "cafe", "fuel", "petrol", "pharmacy",
   "gym") → categories. Shipped as ordinary rules the user can edit or delete, so there is
   no hidden layer that behaves differently from a rule they wrote.

**Suggestions are the real feature.** Grouping uncategorised transactions by merchant key —
the same grouping §17 uses for detection — yields proposals like *"CALTEX FUEL ×14 →
Transport"*. Accepting one writes a normal rule and re-runs categorisation. This turns a
chore into a review.

**The bar.** Coverage is a measured number, not a feeling: the acceptance target is **≥80%
of transactions categorised, by count and by dollar value**, checked against a known
fixture import. A budget view that drops a third of real spend cannot answer "am I on
track", and that is the only reason this app exists.

---

# Part VI — The UI layer

## 25. Shell

```
┌────┬──────────────────────────────────────────────────────────┐
│    │  ●●●●●●●●●●●●●●●●●●●●●●●●●●●●●●●●●●●●●●●●●●●●●●●●●●●●●●●●│  dot grid
│ 76 │  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  · │
│ px │  ┌─────────────────┐  ┌──────────────────┐              │
│icon│  │ widget          │  │ widget           │              │
│rail│  └─────────────────┘  └──────────────────┘              │
│    │  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  ·  · │
│    │            ↓ drag past the fold, canvas grows ↓          │
└────┴──────────────────────────────────────────────────────────┘
```

- **Nav rail** — 76px, icon plus micro caption, collapses to 0 on `Ctrl+\`. Lists the user's
  canvases by name, with a fixed **Settings** entry at the bottom.
- **Header** — 60px. The canvas name and one overflow menu. Deliberately thin.
- **Command palette** `Ctrl+K` and **global search** `Ctrl+F`, across all canvases.
- **Fluid scale** — window width 1280→2400px maps to 0.92×→1.16×, debounced (§34).
- **All options are popout menus** (§27). The header stays a title and a `⋯`, because that
  is the only chrome a canvas gets.

## 26. Canvases

There is no fixed Dashboard/Analytics/Subscriptions split. There is a **canvas list**, and
the user makes canvases and names them — "Bills", "Runway", "Tax", "Fun money".

| | |
|---|---|
| **Create** | From the rail's `+` or the palette. Asks for a name; optionally copies an existing layout. |
| **Name** | Free text, editable from the canvas overflow. |
| **Duplicate** | A canvas as a starting point — templates, not fixed screens. |
| **Delete** | Refused when it is the last one. |
| **Reorder** | Drag in the rail. |

**The invariant:** always at least one canvas, always exactly one Settings entry. Deleting
the last canvas is impossible — the action is disabled with a reason, and Settings has no
delete. Everything else is the user's to shape.

**First run:** one canvas named **Dashboard** with the default widget set, plus Settings.

**Affinity.** Widgets are one global library; any canvas can hold any widget. The
**Glance** / **Depth** labels only seed sensible defaults and order the widget tray:
glance widgets into the first canvas, depth widgets into one created from an "Analysis"
template.

**The surface.**

- **Dot grid** — a dot at every intersection, painted with `QPainter`, no CSS, no image.
  Faintest ink token at rest, one step brighter in edit mode, so the grid becomes legible
  exactly when it matters.
- **Pitch** follows the grid unit: 24px rows, and the column pitch dividing usable width into 12 columns.
- **Vertically unbounded** — dragging past the bottom extends the canvas, with ~480px of
  headroom below the last tile so there is always somewhere to drop.
- **Horizontally bounded** — columns fixed to the window width; no horizontal scrolling. A
  canvas that needs more width needs a new canvas.
- **Empty space is a feature** — blank grid below the last tile is normal, not a layout bug.

**Persistence.** `settings.canvases` is an ordered list of
`{id, name, widgets: [{widget, x, y, w, h, config}]}` plus `canvas_active`.

## 27. Tiles, bands and edit mode

**Grid.** Fixed columns, free resize, unbounded rows. 12 columns × 24px at ≥1100px window
width; 6 columns below. Resizing is continuous and snaps to units on drop.

**Tiles are cards**, matching the app's card vocabulary (`BG_CARD`, 1px `BORDER`, `RADIUS` 12):

| State | Treatment |
|---|---|
| Rest | `BG_CARD`, 1px `BORDER`, `RADIUS` 12 |
| Hover | `BG_HOVER`, or a `BORDER_LIGHT` edge |
| Focus | `FOCUS` ring on custom-painted surfaces |
| Edit mode | `BORDER_LIGHT` outline, drag handle, resize handle, `×`; grid dots brighten |
| Dragging | 60% opacity ghost at the drop position; neighbours reflow 120ms ease-out |
| Drop | 120ms ease-out settle. No bounce, no elastic. |

**Bands.** Each widget declares which of five bands it renders, and recomposes at each boundary:

| Band | Span | Renders |
|---|---|---|
| Compact | w ≤ 3 | one number, one label, at most one spark |
| Standard | w 4–7 | number + delta, or a small chart |
| Wide | w ≥ 8 | number + delta + series side by side |
| Tall | h ≥ 4 | adds a list under the summary |
| Full | w = 12 | the ledger/table compositions |

A widget must render acceptably at its **minimum** span — that is its resize floor. A
widget lacking data for a band degrades, never lies.

**Edit mode.** `Ctrl+E` or the canvas overflow; exits on `Done` or `Escape`. Drag, resize
and remove are inert outside it. Keyboard parity: arrows move, `Shift`+arrows resize.

**Widget tray.** In edit mode, a bottom tray lists library widgets not on the canvas,
grouped Glance/Depth, with placed widgets shown as such.

**Popouts, not dialogs.** Every editing surface is a popout anchored to its trigger. The
rule: **a popout for a single value or a short form; a dialog only when the task is bulk or
multi-step.** Two tasks survive as dialogs — transaction review (multi-row accept/reject)
and first-run onboarding (a flow, not an option). Everything else — recurrence, tags, note,
person, account, consumable, budget, goal, split — is a popout from the row or glyph that
owns it.

## 28. The data hook

Every widget takes **one callable input** — its *data hook* — and calls it to get its own
numbers. That hook is the app's extension point, and it is why a figure the catalogue
doesn't ship is still a figure the app can show.

```python
@dataclass(frozen=True)
class HookContext:
    store: Store          # read-only facade — no mutation API is exposed
    lens: str             # "week" | "month" | "quarter" | "year"
    start: date
    end: date
    label: str            # human label for the period, e.g. "October 2026"
    today: date           # injected, never read from the clock
    peers: Mapping[str, "WidgetData"]   # sibling widgets on the same canvas

def hook(ctx: HookContext, config: dict) -> WidgetData | None: ...

@dataclass(frozen=True)
class WidgetData:
    value: float | None
    delta: float | None
    series: list[tuple[date, float]] | None
    rows: list[Row] | None
    sign: int | None       # -1/0/+1, for colour pairing
    note: str | None       # e.g. "+12 uncategorised"
```

| Rule | Why |
|---|---|
| **Read-only** | A hook reads the store; it never writes. The store's on-disk shape is a contract the UI does not touch. |
| **Pure** | Same `ctx`, same output. No clocks — `today` is injected. This is what makes output memo-cacheable and screenshots reproducible. |
| **No runtime loading** | Hooks are supplied in code at build time. No plugins directory, no `importlib`, no third-party script executed against a live ledger. |
| **Band-aware, not band-dependent** | A hook may return only what its band can show, but never different *values* per band. |
| **Absence is `None`** | A hook with no data returns `None`; the widget hides and the grid reflows. Never a zero that means "unknown". |

**Custom data-processing widgets.** A derived widget — `x over y`, a delta, a share of
total — is **not a new widget class**. It is an ordinary catalogue widget handed a
different hook:

| Template | Meaning |
|---|---|
| `ratio(a, b)` | `a` over `b`, guarded against a zero denominator |
| `share_of(a, b)` | `a` as a percentage of `b` |
| `delta(a, b)` | `a` minus `b`, sign carried into the label |
| `cumulative(a)` | running total across the period |
| `rolling_mean(a, n)` | `n`-period mean of `a` |

The templates are shorthand, not a closed grammar: anything outside the set is still just a
Python function. **There is no expression parser and no formula box** — see §5.

**Caching.** Because hooks are pure, results are memoised on
`(hook_id, config, lens, anchor, store.version)`. `Store.version` is a monotonically
increasing counter bumped on every write, so cache invalidation is exact rather than
time-based.

## 29. Widget library

| Widget | Bands | Content | Source |
|---|---|---|---|
| **Glance** | | | |
| Hero P&L | Compact/Standard/Wide | The loudest figure, count-up on period change; Wide adds a 5-month sparkline | `period_summary` |
| Period summary | Standard/Wide | In / Out / Net + a period lens | `totals_in_range` |
| Incoming ledger | Tall/Full | Click-to-edit rows, recurrence glyph, due date, paid toggle, priority dot, tag chips | items |
| Outgoing ledger | Tall/Full | Same, expenses | items |
| Weekly P&L | Standard/Wide | Current-period weekly in/out bars | `week_ranges` |
| Due soon / overdue | Compact/Standard | Amber due-soon, overdue token, always label-paired | `bills_in_range` |
| Savings goals | Standard/Wide/Tall | Goal bars, target, projected completion | `goal_eta` |
| Cost summary | Standard/Wide | Subscription count, monthly cost, owed to you | items, `who_owes` |
| Spending calendar | Wide/Tall/Full | Borderless spend/due-dates calendar | transactions |
| Subscription list | Tall/Full | Cost splits, "waiting on" unpaid shares, mark-paid | `member_shares` |
| Uncategorised review | Compact/Standard | "54 uncategorised · review 9 suggested rules" | transactions, `suggest_rules` |
| Renewal timeline | Wide/Full | Next-60-days dot-plot | `upcoming_renewals` |
| Untracked charges | Standard/Wide | Recurring charges absent from the ledger | `detect_subscriptions` |
| People roster | Tall/Full | Per-person owed/paid + settle-up | `people_roster` |
| Owed to you | Compact/Standard | Rollup + top people | `who_owes` |
| Consumables | Tall/Full | Shared one-off/recurring purchases | tracker |
| Cost-per-use | Standard/Wide | Cost per use / day / year | `tracker_cost_per_*` |
| Predicted income | Standard/Wide/Tall | Next-month income + source list | `predicted_income` |
| Net worth | Compact/Standard/Wide | Value, delta, mini series | `net_worth_series` |
| Budget pace | Standard/Wide | Per-category on-track/over, no chart | `budget_vs_actual` |
| **Depth** | | | |
| Metric strip | Full | Avg income/expenses/P&L/savings, each with a sparkline | `period_summary` |
| Record months | Wide/Full | Best/worst/months-positive/income-growth — **colour by sign, never by rank** | `history` |
| P&L trend | Standard/Wide/Tall | Interactive; click filters a sibling donut | `history` + peers |
| Category donut | Standard/Wide | Sign-based ramp, not `SERIES` | transactions |
| Month-by-month table | Wide/Tall/Full | One row per month | `history` |
| Budget vs actual | Full | Grouped bullet bar; empty state when no budgets | `budget_vs_actual` |
| Liquid-balance forecast | Full | 6-month projection band | `forecast_band` |
| Spending composition | Wide/Full | Stacked bar, `SERIES` | transactions |
| Plan vs actual | Standard/Wide | Recurring plan vs reconciled actual | `plan_summary` |
| Goal projection | Standard/Wide/Tall | Expandable projection per goal | `goal_eta` |
| Cumulative savings | Standard/Wide/Full | Area chart | `history` |
| Savings record | Wide/Full | Total saved / avg / best month / months tracked | `history` |
| Spend by tag | Standard/Wide | Tag breakdown | transactions |
| Daily spend | Standard/Wide | Day-resolution spend | transactions |
| Budget report | Wide/Full | Full budget-vs-actual detail | `budget_vs_actual` |

## 30. Theme

**The BuildNexus design system** — IBM Carbon v11 tokens with Primer's corner radii — in
its **Gray 100** dark theme. Every value in `ui/theme.py` is the system's own, and each line
names the token it is, so a value can be checked against the system by name rather than by
memory. A test asserts the whole table.

The token layer follows Carbon's layer model rather than custom values: one base
`background`, cards on `layer-01`, nested content on `layer-02` and `layer-03`.

| Layer | Choice |
|---|---|
| **Colour** | Carbon Gray 100 neutrals: `background` → `layer-01` → `layer-02` → `layer-03`. |
| **Type** | Carbon's named styles — `body-compact-01`, `label-01`, `heading-compact-01`, `heading-03` … `heading-06`. IBM Plex Sans Light / Regular / SemiBold, fixed at 300/400/600: **no 700**, as the system says explicitly. Bundled as TTF/OTF because Qt cannot load the system's woff2. |
| **Spacing** | Carbon's 8px mini-unit: 16 inside components, 32 between cards, 64 between page sections. |
| **Radius** | Primer's four steps, split by *element* rather than by size: 3 for checkboxes and counters, 6 for buttons, fields **and cards**, 12 for dialogs, popovers and menus, full for pills. |
| **Motion** | 110ms hover/press, 240ms menus and view changes, 400ms page-sized, 700ms counting numbers. Enter decelerates, leave accelerates. |
| **Focus** | A 2px ring, white in Gray 100, on every custom-painted surface; filled controls add the system's 1px `focus-inset` ring inside it. |

**Content follows the system too.** Sentence case everywhere — buttons, titles, menu items,
table headers — with the reader addressed as *you*, actions led by a verb, numbers as
numerals, and no exclamation marks or emoji. Error messages say what happened and what to do
next. This is a *rule of the system*, not a preference, so it is checked mechanically: a test
scans every string the UI can display and fails on a shout-cased label.

**Semantic hues are the app's own.** Carbon's `support-success` (#42be65) and
`support-error` (#fa4d56) are high-saturation; the app's muted green, terracotta and amber
are deliberately quieter, to serve "calm under bad news". The system's *rules* are adopted;
its *status hues* are not, and a test asserts the muted hues really are quieter. Status is
always colour **and** a word, never colour alone. Two exceptions come straight from Carbon
because their whole message is severity: `CAUTION` (`support-caution-major`) and `INFO`
(`support-info`).

**Depth is layering, with the system's one exception.** BuildNexus reserves
`shadow-overlay` for menus, dropdowns, popovers and modals — the only raised elements it
allows. A popout is exactly that, so it casts the shadow; the palette and everything else
separate by a layer change or a hairline, which the system also prescribes. Painting the
shadow needs a margin the palette's geometry does not have to spare, and inverting its
row-painting, hit-testing and sizing for a subtle effect was not worth it — so the palette
keeps the border, noted here rather than left to be noticed.

**Two local extensions**, kept deliberately: `SERIES`, an 8-step categorical palette used
only for multi-category breakdowns, and `GRID`, a chart gridline token fainter than any
divider — which also becomes the canvas dot colour.

**Everything is hand-painted.** Charts are drawn with `QPainter` against these tokens — no
charting library, no default palette. Icons are drawn as vectors, not shipped as assets.
That keeps the visual language consistent with the theme at any scale and keeps the build
free of image dependencies.

## 31. Keyboard

| Action | Result |
|---|---|
| `Ctrl+K` / `Ctrl+F` | Palette / search across all canvases |
| `Ctrl+E` | Edit mode |
| Arrows, `Shift`+arrows | Move / resize a tile in edit mode |
| `Tab` / `Shift+Tab` | Move between fields; a row can be tabbed straight into its amount |
| `Escape` | Exit edit mode, close a popout, clear a filter |

Focus is always visible (§30's focus ring) and every action reachable by mouse is reachable
by keyboard — which is both the accessibility floor and what makes the app feel like the
terminal it's trying to be.

## 32. Key states

| State | Behaviour |
|---|---|
| **First run** | The seeded canvas with default widgets; one teaching block if there's no data — not fourteen empty tiles. |
| **Widget with no data** | Hides itself; the grid reflows. Never holds space as an empty tile. |
| **Partial data** | One point in a trend: show the number, suppress the series. |
| **Resize below floor** | Snaps back with a 120ms ease-out. No error, no dialog. |
| **Narrow window** | Below 1100px the grid drops to 6 columns and tiles step down a band. 12-column geometry is retained so widening restores the layout. |
| **Last canvas** | Delete is disabled, reason shown in the menu item. |
| **Corrupt or missing layout** | Seed a default canvas; one note in Settings. Never blank. |
| **Sync failed** | An explicit "reconnect required" — never a silent "no new transactions" (§22). |
| **Import partially parsed** | "412 read, 7 skipped" — counted and shown, never silent. |

## 33. Accessibility

- AA contrast on every colour pairing, verified against the tokens rather than by eye.
- Sign is carried by colour **and** a `+`/`−` prefix, so no state depends on hue alone.
- Focus rings on all custom-painted surfaces, which are exactly the places Qt won't draw one.
- The palette and search are the keyboard path to everything; no action is mouse-only.

## 34. Fluid scale and headless shots

**Fluid scale.** A single `apply_ui_scale()` sets a global factor from window width and
re-fits everything that reads it; every label keeps its base size so text can be resized
proportionally without compounding. Debounced, because re-laying-out on every resize event
is the classic way to make a Qt app feel heavy.

**`--shot`.** A headless mode that renders a named surface at a named size and writes a PNG.
This is the app's visual regression harness and the reason every animation lives behind an
`ANIMATE` flag: with `ANIMATE` off, a screenshot is deterministic.

---

# Part VII — Delivery

## 35. Build order

| Phase | Work | Gate |
|---|---|---|
| **0 — Foundations** | Repo layout, the layering rule, the theme token module, data model dataclasses, store with atomic writes and `schema_version`. | The store round-trips a fixture document; a test proves no engine module imports Qt. |
| **1 — Engine** | Recurrence, periods, ledger totals, then analytics, goals, shared, tracker, detection, search — each with tests before the next begins. | Every function returns correct values for a fixture store with hand-computed expectations. |
| **2 — Import** | Normalisation, CSV column detection, OFX, identity and dedupe, categorisation rules and suggestions, reconciliation. | A fixture statement imports end-to-end; dedupe discards a re-imported file exactly; categorisation coverage ≥80% by count and value (§24). |
| **3 — Sync** | SimpleFIN `claim` / `fetch_accounts` / `sync` with a stubbed HTTP layer; incremental high-water mark; revocation and expiry states; the settings flag. | Sync against the stub is idempotent — running it twice imports nothing the second time. |
| **4 — Shell** | Nav rail with the canvas list, header, stack, palette, search, fluid scale, `--shot`. One seeded canvas, no widgets. | Launches; creates, renames and deletes canvases; refuses the last delete. |
| **5 — Canvas** | Dot grid, vertical extension, tile primitive, drag/resize, bands, layout persistence, edit mode, tray. | A dummy widget can be added, moved past the fold, resized, removed, and survives restart. |
| **6 — Widgets** | The catalogue (§29) in value order: hero, period summary, ledgers, due-soon, metric strip, then charts — each behind a data hook. | Each widget's numbers match the engine's for the same fixture, and `--shot` gives a visual diff per surface. |
| **7 — Popouts, settings, onboarding** | Every editing surface from §27; the sectioned Settings form; the first-run flow. | Every option is reachable from its trigger's menu. |
| **8 — Harden** | Empty, partial, corrupt and sync-failure states (§32); accessibility pass (§33). | Runs clean on real data for a month without a surprise. |

**The gates are checks, not vibes.** Phases 1–3 pass when a fixture store produces
hand-computed numbers. Phases 6–8 pass when, for the same fixture, the new surface
produces the same figures as the engine says it should.

## 36. Testing

| Level | Covers | How |
|---|---|---|
| **Engine unit tests** | Every function in §14 | Fixture stores with hand-computed expected values. The bulk of the suite. |
| **Importer tests** | csv, ofx, column detection, edge cases | Fixture files including a headerless export and preamble junk. |
| **Sync tests** | claim, mapping, dedupe, `402`/`403` | A stubbed HTTP layer. Network code with no tests is the single largest risk in this plan. |
| **Hook tests** | Purity and templates | Same context twice produces identical output; a hook never mutates the store. |
| **Visual shots** | Layout, bands, states | `--shot` PNGs, `ANIMATE` off, diffed per surface. |

The engine's testability is the deliverable that makes everything else safe to change —
which is why §6's dependency rule is stated as architecture rather than preference.

## 37. Risks

The cost of building this isn't the code it writes — it's the latent detail it forgets:

- **Month-end clamping.** A monthly item on the 31st must fire in February. Gets it wrong once, loses trust permanently.
- **Dedupe.** See §21. The failure is silent, and silence is the worst possible failure for a reconciliation tool.
- **Categorisation coverage.** If a third of spend lands in "Uncategorised", budget-vs-actual is a lie and the app has no reason to exist.
- **Failed sync looking successful.** A lapsed consent must never render as "nothing new".
- **Occurrence-level edits.** Splitting a series forward without rewriting the past is the subtlest logic in the app; it needs the most tests.
- **Fluid scale.** The 1280→2400px band, debounced, with a base size per label.
- **Motion vocabulary** — crossfade, row fade-in, hero count-up, chart reveal — all behind `ANIMATE`, or screenshots stop being deterministic.
- **Accessibility.** AA contrast, sign by colour *and* prefix, focus rings on custom-painted surfaces.
- **Atomic writes.** A crash during a save must not corrupt the store.

## 38. Open decisions

- **Fonts.** IBM Plex Sans TTF (OFL-licensed) must be downloaded and bundled — the design
  system ships woff2 only, which Qt cannot load. Until then the app falls back to a system font.
- **Band count.** Five bands per widget is a lot to author per widget. Two (compact /
  expanded) with opt-in extras is cheaper and probably enough; §29 currently claims five.
- **`SERIES` and `GRID`.** Retained as local extensions because the design system supplies
  no data-viz palette. Worth revisiting only if a vetted categorical palette turns up.
- **CAMT.053.** The one format ANZ can produce on request, and the app handles only CSV and
  OFX. An afternoon's work, worth doing only if such a file ever actually arrives.
- **Investments.** Deliberately out of scope (§5), with its own plan. Revisit only if
  investments are actually held.
- **Net-worth snapshots.** Whether net worth is captured automatically on a schedule or only
  on explicit action is undecided; it determines whether the series has gaps.
- **Doc set.** This document, [PRODUCT.md](../PRODUCT.md) and
  [buildnexus-mapping.md](buildnexus-mapping.md) are current; [DESIGN.md](../DESIGN.md)'s
  design-system section still documents a pre-Carbon square-corner system (0px radius,
  Arial Nova) that §30 has replaced. Fold the docs into three and delete the rest.
