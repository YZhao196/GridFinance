# Frontend specification

**Status: there is no frontend.** It was deleted on 2026-10-08 to be redesigned. This is the
specification for the one that replaces it — every behaviour, every dimension, every widget,
written out.

It is not a description of code that exists. It is what a new frontend must do, derived from
three sources that do exist: `design-doc.md` (the product spec), `BACKEND.md` (the contract a
frontend reads), and `hooks/library.py` (the 35 hook ids this frontend binds to).

Read with `BACKEND.md` open. Where this document names a figure, the engine computes it —
the frontend renders, it does not calculate. That is §1's law and the reason the numbers on
two different widgets can never disagree.

---

## 1. What the backend hands you

Nothing here needs re-deriving. Every widget is one `@hook("id")` call through
`hooks/cache.py`, which memoises on `(hook_id, config, lens, anchor, store.version)`.

```python
WidgetData(
    value:  float | None,              # the one big number
    delta:  float | None,              # change vs the previous period
    series: list[tuple[date, float]] | None,
    rows:   list[Row] | None,
    sign:   int | None,                # -1/0/+1, for colour; derived from value if unset
    note:   str | None,                # a short trailing remark
    extra:  dict[str, Any],            # per-widget; see the catalogue below
)

Row(
    label: str,
    value: float | None,
    detail: str,                       # the secondary text on the right
    sign: int | None,
    tags: tuple[str, ...],             # render as chips
    target: dict,                      # usually a slice/segment the row came from
    reference: dict,                   # what to OPEN when the row is activated
)
```

Five rules the frontend must respect, because the backend relies on them:

| Rule | What it means for you |
|---|---|
| **`None` is absence, not zero** | `WidgetData.is_empty` is what hides a tile. A `rows=[]` is *not* empty — it is a considered empty state you must render. Never draw a `0` where the answer is "unknown". |
| **One point is not a trend** | `has_series` is false below two points. Show the number, suppress the chart. |
| **Never compute a competing figure** | If you want a percentage, ask the hook for it. Two paths to one number is the §1 failure. |
| **`reference` is enough to act** | Activating a row should open what `reference` names without re-deriving what the row meant. |
| **Hooks are pure and cached** | The same inputs give the same output. Your render must be a function of `WidgetData`, or the cache will show you the wrong thing. |

`reference["kind"]` is one of: `item`, `person`, `consumable`, `category`, `month`, `goal`,
`charge`, `suggestion`, `slice`, `period`. Each maps to one editing surface (§6.3).

---

## 2. Chrome

Three fixed pieces, and nothing else. **Every option is a popout** (§27) — the app has no
menubar, no toolbar, no status bar, no modal settings dialog.

### 2.1 The nav rail

| | |
|---|---|
| **Width** | 76px at rest. Collapses to 0 on `Ctrl+\`. |
| **Contents** | One entry per canvas, in the user's order, then a `+` (create), then **Settings**, pinned to the bottom. |
| **Entry** | 44px tall, 8px horizontal inset. A Carbon icon, 20px, above a caption in `label-01` (12px), centred, one line, ellipsised. |
| **Caption** | The canvas name **as the user typed it** — never transformed, never uppercased. |
| **Icon** | Chosen from the canvas *name* by keyword (`Bills` → receipt, `Runway` → trend). A name with no keyword gets a generic grid. |
| **Active** | `BG_SELECTED` fill, `RADIUS_MEDIUM` (6px), a 2px `PRIMARY` accent bar on the left edge, caption and icon in `TEXT`. |
| **Hover** | `BG_HOVER` fill, ink one step brighter. |
| **Focus** | The 2px `FOCUS` ring, inset 8px, drawn around the entry. |
| **Reorder** | Drag an entry vertically. The insertion point is shown as a 2px `PRIMARY` line between entries. Reorder commits on drop, one write. |
| **Right-click** | Opens a popout: Rename…, Duplicate, Delete. Delete is **disabled with a reason in the item** when it is the last canvas. |
| **Keyboard** | `Tab` reaches the rail; `↑`/`↓` walk entries and *select* as they go; `Enter` activates. |

### 2.2 The app header

**60px tall, full width, `BG_APP`.** Deliberately thin: it is the only chrome a canvas gets.

| Position | Element | Behaviour |
|---|---|---|
| Left | **Canvas name**, `heading-04` (24px, weight 400, `TEXT`) | The active canvas's name. Click to rename in place — an inline text field, `Enter` commits, `Escape` reverts. |
| Right, first | **`‹` period step** | Steps back one unit of the current lens. 32px hit target. Hover `BG_HOVER`. |
| Right, middle | **Period label**, `body-compact-01` (14px, `TEXT_SECONDARY`) | e.g. `October 2026`. **Clicking it opens the lens menu** — week / month / quarter / year — plus *Today*. |
| Right, next | **`›` period step** | Same, forward. |
| Right, last | **`⋯` overflow** | The canvas menu: Edit layout (`Ctrl+E`), Add widget, Rename…, Duplicate, Delete. |

Rules:

- The stepper and the label sit **together**, not as separate chrome, because every widget
  shares one lens (§28) — the lens has to be visible and adjustable somewhere, and anywhere
  else would make it a second header.
- The header shows the **period the whole canvas is looking at**, not a per-widget period.
- When a canvas widget is filtered by a sibling (P&L trend → donut), the header says so:
  a small chip after the period label reading `Filtered: 2026-06`, with an `×` to clear.
- No button in the header is eager. Nothing in the header writes without a popout.

### 2.3 The command palette

`Ctrl+K` for commands, `Ctrl+F` for search. One surface, two modes.

| | |
|---|---|
| **Size** | Width 560px, centred horizontally, 20% from the top. `RADIUS_LARGE` (12px). `SHADOW_OVERLAY`. |
| **Field** | 44px tall, no border, `body-compact-02` (16px). A `search` icon at 16px, left. |
| **Results** | 34px rows. Label left, group right in `label-01` `TEXT_MUTED`. **Matched characters in `HIGHLIGHT`** (`#001d6c`) — not bold, not underline. |
| **Keys** | `↑`/`↓` move, `Enter` runs, `Escape` closes. The highlighted row is a `BG_SELECTED` fill. |
| **Empty** | One line: `No commands match "xyz".` — never a blank panel. |

Search covers **all canvases**, not just the visible one, and results carry the canvas name.

---

## 3. The grid

The canvas is a grid of tiles. This section is the geometry; §4 is what goes inside one.

### 3.1 Geometry

| Quantity | Value | Why |
|---|---|---|
| **Columns** | **12** at ≥1100px window width, **6** below | A tile's `x`/`w` are stored in the 12-column space and *not rewritten* when the window narrows, so widening restores the layout exactly. |
| **Row height** | **24px** | The vertical unit. A tile's height is a multiple. |
| **Column gap** | **16px** | Between tiles. |
| **Row gap** | **16px** | Between tiles. |
| **Outer margin** | **32px** left/right, **24px** top | `spacing-07`. |
| **Minimum tile** | **3 columns × 6 rows** for a text figure; per-widget floors in §6 | Below the floor the tile snaps back on drop (§8). |

A tile's pixel rect is:

```
column_width = (usable_width - 11 * 16) / 12
x_px         = margin + column * (column_width + 16)
width_px     = w * column_width + (w - 1) * 16
y_px         = margin + row * (24 + 16)
height_px    = h * 24 + (h - 1) * 16
```

### 3.2 The dot grid

- **A dot at every grid intersection.** Painted, not a background image, not CSS.
- **Radius 0.9px**, colour `GRID` (`#1f1f21`) — the faintest ink in the app, deliberately
  fainter than any divider.
- **In edit mode** the dots switch to `GRID_EDIT` (`#3d3d3f`), one step brighter. The grid
  becomes legible exactly when it matters and is nearly invisible when it doesn't.
- **Dots never appear inside a tile.** A tile paints its own `BG_CARD` fill over them.
- **The dots are not interactive.** They are a ruler, not a target.

### 3.3 Vertically unbounded, horizontally bounded

- **Down is free.** Dragging a tile past the bottom of the visible area extends the canvas.
- **Headroom.** ~480px of droppable space is always kept below the last tile, so there is
  somewhere to drop without first scrolling. It shrinks to that minimum, never to zero.
- **Across is fixed.** A tile cannot be dragged beyond column 12 (or 6). There is no
  horizontal scrolling, ever. A canvas that needs more width needs another canvas.
- **Empty space below the last tile is normal** — the dot grid continues. It is not a
  layout bug and must not be "fixed" by centring or stretching anything.

### 3.4 The band table

A tile recomposes at five boundaries. **A band is a different composition, not a scaled one**
— the same widget at Compact and at Wide share no layout, only data.

| Band | Condition | Renders |
|---|---|---|
| **Compact** | `w ≤ 3` | One number, one label, at most one spark. No axis, no legend, no rows. |
| **Standard** | `w` 4–7 | Number + delta, or a small chart. |
| **Wide** | `w ≥ 8` | Number + delta + series, side by side. |
| **Tall** | `h ≥ 4` | Adds a list under the summary. |
| **Full** | `w = 12` | The ledger and table compositions. |

Rules:

- **Tall and Full compose with a width band**, not instead of one. A 12-wide, 5-tall tile is
  *Wide + Tall + Full* and renders all three behaviours.
- **Band boundaries are inclusive at the lower edge**: exactly 3 columns is Compact, exactly
  4 is Standard, exactly 8 is Wide.
- **A widget must be acceptable at its floor.** That floor is the smallest span where it
  still says something true. Below it, the drop snaps back.
- **Degrade, never lie.** If a band needs a series and only one point exists, show the number
  and drop the chart. If a band needs rows and there are none, render the widget's empty
  state — *not* a blank tile.

---

## 4. Tiles

A tile is a **card**: `BG_CARD` fill, a 1px `BORDER` outline, and **`RADIUS_MEDIUM` — 6px**.

> `design-doc.md` §27 says `RADIUS` 12 for tiles. That predates the BuildNexus work, which
> classifies a card as *medium* and reserves 12 for dialogs, popovers and menus (§30). **6 is
> correct**; the spec line is stale.

### 4.1 The tile header row

Every tile has one, at the top, inside the padding. Three slots, left to right.

| Slot | Contents | Type | Alignment |
|---|---|---|---|
| **Left** | The widget's name, sentence case, exactly as the catalogue spells it (`Due soon / overdue`, not `DUE SOON / OVERDUE`) | `label-01` — 12px, weight 400, `TEXT_MUTED` | Left |
| **Right** | The widget's `note` from `WidgetData`, when there is one (`+12 uncategorised`, `3 subscriptions`) | `label-01`, `TEXT_MUTED` | Right |
| **Far right, edit mode only** | Drag handle (6-dot grip), then `×` | `TEXT_MUTED`, 16px | Right |

Behaviours:

- **The header row is skipped entirely when the tile is too short** — if the tile cannot fit
  the header *and* one line of content, spend the pixels on the figure. A widget squeezed to
  its floor should not caption itself.
- **Height** is one line of `label-01` (16px) plus `spacing-01` (2px).
- **`×` removes the tile immediately.** It is not destructive to data — the tile is a view —
  so it does not confirm. `Ctrl+Z` does not restore it; the tray does.
- **The grip is the drag target for the whole tile** in edit mode, but the *body* is also
  draggable so the user does not have to aim.
- **In edit mode the header is always shown**, even on a short tile, because that is where
  the affordances live.

### 4.2 The body

The rest of the tile, below the header, inside the padding.

- **Padding:** `spacing-05` (16px) on all four sides, minus whatever the band budget allows at
  Compact, where it drops to `spacing-03` (8px).
- **The figure is `heading-04` (24px) at Wide, `heading-03` (20px) at Standard, and `FS_MICRO`
  in a strip at Compact**, set in IBM Plex Mono with tabular figures so digits do not shift
  as they count.
- **Sign is colour *and* a glyph.** A negative figure always prints `−` (U+2212, not a
  hyphen). Green/red are the app's muted hues, never Carbon's saturated ones (§30).
- **A figure that would overflow its tile clamps its type rather than escaping** — the tile
  is the container, the number is the guest.

### 4.3 States

| State | Treatment |
|---|---|
| **Rest** | `BG_CARD`, 1px `BORDER`, 6px radius. |
| **Hover** | `BG_HOVER` (`#333333`), border to `BORDER_LIGHT`. Cursor becomes a pointer if the tile is clickable. |
| **Focus** | A 2px `FOCUS` ring (white in Gray 100), drawn *by you* — Qt will not draw one on a custom-painted widget. |
| **Dragging** | A 60% opacity ghost of the tile follows the cursor. The original stays put until drop. |
| **Drop target** | The ghost snaps to the nearest free cell and shows its outline. Legal targets are highlighted; illegal ones are not offered. |
| **Settling** | 120ms `OutCubic` ease-out, no bounce, no elastic. Neighbours reflow in the same 120ms. |
| **Empty** | The tile **hides itself** (`WidgetData.is_empty`). The grid reflows to close the gap. Never a placeholder, never an empty frame. |
| **Considered empty** | `rows=[]` is *not* empty — render the explanatory state the hook intends. |

### 4.4 Edit mode

Entered with `Ctrl+E` or the overflow menu; exited with `Done` or `Escape`.

- Drag, resize and remove are **inert outside edit mode**. A click on a tile in normal mode
  acts on the tile's data, never on the tile.
- **Resize** by dragging any edge or corner; the tile snaps to whole cells on drop.
- **Below the floor** the tile snaps back to its minimum with the 120ms ease-out. No error,
  no dialog, no explanation.
- **Keyboard parity:** arrows move the focused tile one cell; `Shift`+arrows resize it.
- **The tray** appears along the bottom in edit mode: every library widget not on this canvas,
  grouped `Glance` / `Depth`, with the ones already placed shown as such and not draggable.
- Tiles already placed show their header affordances; tiles not placed show only a name.

---

## 5. Data grids

Two places need a real table: the **ledger tiles** (incoming/outgoing, at Tall and Full) and
the **Sheets page**. They share one column grammar, because a ledger that reads differently
from a spreadsheet of the same rows is a bug in the reader's head.

### 5.1 The column header row

| | |
|---|---|
| **Height** | 32px. |
| **Background** | `BG_SOFT` (`#393939`). |
| **Border** | None except a 1px `BORDER` line along the **bottom** edge only. |
| **Label type** | `label-01` — 12px, weight 400, `TEXT_MUTED`. |
| **Alignment** | **Left, always** — including over right-aligned numeric columns. The header names the column; the numbers align to their decimal. |
| **Case** | **Sentence case** (`Amount`, `Due`, `Paid`, `Cancelled`). Never `AMOUNT`. |
| **Padding** | 6px vertical, 8px horizontal. |
| **Sticky** | The header row does not scroll vertically with the body; it stays pinned. |
| **Sort** | Clicking a header sorts by it, ascending, toggling to descending on the next click. The sorted column shows a `chevron_down`/`chevron_up` 16px after the label. Sorting is **view state only** — it never rewrites the store. |
| **Stretch** | Exactly one column takes the slack — the descriptive one (Description, Item, Match). Every other column has a fixed width. A grid whose columns stop two-thirds across the window looks broken. |

### 5.2 Rows

| | |
|---|---|
| **Height** | 34px comfortable, 26px tight, chosen by the band. |
| **Gridlines** | Horizontal hairlines in `BORDER`; no vertical rules. |
| **Hover** | `BG_HOVER` on the whole row, including the gutters. |
| **Selected** | `BG_SELECTED` (`#393939`), text unchanged. Selection is by row, not by cell. |
| **Money** | Right-aligned, mono, tabular figures, two decimals. `−` for negatives. |
| **Chips** | Tags render as pills: `RADIUS_FULL`, `BG_TAG`, `label-01`, 4px horizontal padding. |
| **Overflow** | `+19 more` in `TEXT_MUTED`, right-aligned, as the last row — never a scrollbar inside a tile. |

### 5.3 Activating rows and cells

- **A row is a target.** Clicking it opens the editor its `reference` names (§6.3). This is
  the whole point of `reference`: the row knows what it is, so the frontend does not guess.
- **The whole row is the hit area**, not the label. A 4px-wide text run is not a button.
- **Double-click a cell to edit it in place** — a text field for text, a picker for a
  category or account, a checkbox for a boolean. `Enter` commits, `Escape` reverts, `Tab`
  moves to the next editable cell in the row.
- **Editing commits through the store**, so `store.version` moves and every other tile
  re-reads. A grid that wrote around the store would leave the canvas showing stale numbers.
- **A cell that cannot be edited is not editable.** Do not accept a keystroke, store it, and
  change nothing — the row's derived fields (an occurrence's date, an item's type) are
  read-only and must look it.
- **Deleting asks first.** An edit can be undone; a delete cannot. The dialog names the row.

---

## 6. The catalogue

35 widgets. Each is one `@hook("id")`. **Bands are composition, not scaling** — a widget
listed at Compact and Wide refers to two layouts.

Floors below are the smallest span at which the widget still says something true.

### 6.1 Glance — the everyday canvas

| Widget | Hook | Bands | Floor | What it shows, and what you can do |
|---|---|---|---|---|
| **Hero P&L** | `hero_pl` | C / S / W | 3×3 | The loudest figure on the canvas: net for the period, count-up on period change (`MS_COUNT` 700ms), `note` under it (`net this period`), and the delta vs the previous period with a sign glyph. **Wide adds a 5-month sparkline** from `extra.series`. Clicking opens the period lens menu. |
| **Period summary** | `period_summary` | S / W | 4×3 | Three figures in a row: `In` / `Out` / `Net` from `extra.income`, `extra.expenses`, and `value`. Neither the label nor the figure is ever uppercased. The delta sits under `Net`. Clicking a figure opens the ledger that feeds it. |
| **Incoming ledger** | `incoming_ledger` | T / F | 6×4 | A data grid (§5) of income occurrences. Columns: Date · Item · Amount. Each row carries a recurrence glyph when the item repeats, its due date, a paid toggle, a priority dot and tag chips. **Activating a row opens that item's occurrence popout.** At Full it names every row; at Tall it shows as many as fit and ends with `+N more`. |
| **Outgoing ledger** | `outgoing_ledger` | T / F | 6×4 | The same, expenses, with the total at the top right. |
| **Weekly P&L** | `weekly_pl` | S / W | 4×3 | Paired in/out bars, one pair per week of the period, from `extra.series` and `extra.weeks`. **Hovering a bar shows that week's dates and both figures** in a tooltip on `BG_INVERSE`. Clicking a bar retargets the header's period to that week. |
| **Due soon / overdue** | `due_soon` | C / S | 3×3 | Amber count of what is due within 7 days, and a red token for what is already overdue. **Colour and word always together** — `3 due soon`, never a bare coloured dot. Below, two figures: `Overdue $3.5k` and `Due soon $98`. Clicking opens the filtered ledger of exactly those rows. |
| **Savings goals** | `savings_goals` | S / W / T | 4×3 | One bar per goal: name, `saved of target`, projection. At Tall, up to four goals then `+N more`. **Each bar is a row**: clicking opens that goal's projection popout. |
| **Cost summary** | `cost_summary` | S / W | 4×3 | `monthly` figure, then `Count` / `Yearly` / `Owed` in a footer strip. Clicking `Count` opens the subscription list; `Owed` opens the people roster filtered to debtors. |
| **Spending calendar** | `spending_calendar` | W / T / F | 8×4 | **Borderless.** Seven columns of weekdays, one cell per day, tinted by spend intensity from `extra.days`; days with a bill due carry a marker, and `extra.peak` sets the scale. Hovering a day shows `12 Oct · −$145.00`. Clicking a day opens that day's transactions. |
| **Subscription list** | `subscription_list` | T / F | 6×4 | Data grid of recurring costs: name, monthly equivalent, next due. Rows show unpaid shares as `waiting on Sam`, and offer a **mark-paid** action in the row. Activating a row opens the item. |
| **Uncategorised review** | `uncategorised_review` | C / S | 3×3 | One sentence from `note`: `54 uncategorised · review 9 suggested rules`. The number is a **link** — clicking opens the review dialog, which is one of the two surviving dialogs (§27) because it is a bulk accept/reject task. |
| **Renewal timeline** | `renewal_timeline` | W / F | 8×4 | A dot-plot of the next 60 days, one dot per renewal, dot size by amount. `extra.total` heads it. Hovering a dot names the renewal and its date; clicking opens the occurrence. **Leaves only** — a category node holding children is not a renewal. |
| **Untracked charges** | `untracked_charges` | S / W | 4×3 | Recurring charges the ledger does not know about, monthly and yearly estimates. Each row offers **Track this** — one click creates the item. Until then nothing is written. |
| **People roster** | `people_roster` | T / F | 6×4 | One row per person: outstanding, direction (`owes you` / `you owe`), settled-up state. Rows carry a **settle up** action. Activating opens that person's popout. |
| **Owed to you** | `owed_to_you` | C / S | 3×3 | The rollup figure plus the top two or three people. Clicking opens the roster. |
| **Consumables** | `consumables` | T / F | 6×4 | Data grid of shared one-off and recurring purchases: name, quantity, total. Activating a row opens the consumable. |
| **Cost-per-use** | `cost_per_use` | S / W | 4×3 | Cost per use / per day / per year, each with its own label. Sorted by cost per use, descending — the expensive-per-use thing is the one to notice. |
| **Predicted income** | `predicted_income` | S / W / T | 4×3 | Next month's expected income as the figure, with the sources listed under it at Tall. Each source row opens the item that produces it. |
| **Net worth** | `net_worth` | C / S / W | 3×3 | Value, delta since the last reading, mini series. Where a snapshot is missing, the series simply starts later — no interpolation, no zero-filling. Accounts in another currency are **flagged in `extra.foreign`, never summed in**. |
| **Budget pace** | `budget_pace` | S / W | 4×3 | Per-category on-track / over, as a list. **No chart** — pace is a judgement, not a curve. Each row opens that category's budget popout. |

### 6.2 Depth — the analysis canvas

| Widget | Hook | Bands | Floor | What it shows, and what you can do |
|---|---|---|---|---|
| **Metric strip** | `metric_strip` | F | 12×3 | Four to six averages side by side, each with its own sparkline, from `extra.metrics` and `extra.months`. Equal columns, no exception. |
| **Record months** | `record_months` | W / F | 8×3 | Best and worst months, months positive, income growth. **Colour by sign, never by rank** — a "best" month that lost money is still red. |
| **P&L trend** | `pl_trend` | S / W / T | 4×4 | A line of net per period. **Interactive:** clicking a point filters a sibling donut or composition on the same canvas. The selected point is marked and `extra.selected` carries it. Clicking empty space clears the filter. This is the one widget that writes to `ctx.peers`. |
| **Category donut** | `category_donut` | S / W | 4×4 | Concentric rings, one per parent category. **Ramp by sign, not `SERIES`** — this is a composition, not a categorical breakdown. Hovering a segment raises it and names it; clicking opens that category. |
| **Month-by-month table** | `month_table` | W / T / F | 8×4 | One row per month: plan in/out/net and actual. A data grid (§5). Each row opens that month in the header. The current month is marked. |
| **Budget vs actual** | `budget_vs_actual` | F | 12×5 | A grouped bullet bar per category: planned track, actual fill, variance. **Considered empty state when no budgets exist** — an explanation, not a blank tile. Rows open the category's budget. |
| **Liquid-balance forecast** | `liquid_forecast` | F | 12×4 | A six-month projection band from `extra.series`, with opening, low, high and a σ band. **The first negative month is marked and labelled** — that is the whole point of the widget. `extra.first_negative` names it. |
| **Spending composition** | `spending_composition` | W / F | 8×4 | A stacked bar over the period, using the `SERIES` categorical ramp. Legend below, in the same order as the stack. Clicking a segment opens that category. |
| **Plan vs actual** | `plan_vs_actual` | S / W | 4×3 | Recurring plan against reconciled actual. Standard shows `Plan in` / `Plan out` / `Plan net`; Wide adds `Actual in` / `Actual out` / `Settled N of M`. Clicking a figure opens the ledger it came from. |
| **Goal projection** | `goal_projection` | S / W / T | 4×4 | One row per goal: saved, target, projected completion. **Expandable** — activating a row opens its month-by-month projection. |
| **Cumulative savings** | `cumulative_savings` | S / W / F | 4×3 | An area chart of running savings across the period, from `extra.series`. |
| **Savings record** | `savings_record` | W / F | 8×3 | Total saved, average, best month, months tracked. |
| **Spend by tag** | `spend_by_tag` | S / W | 4×3 | A tag breakdown, `SERIES` ramp, largest first. Clicking a tag opens every transaction carrying it. |
| **Daily spend** | `daily_spend` | S / W | 4×3 | A day-resolution bar series with `extra.peak` marked. Hovering a bar shows the date and amount. |
| **Budget report** | `budget_report` | W / F | 8×4 | The full budget-vs-actual detail as a data grid: category, planned, actual, coverage. Rows open the category. |

### 6.3 What a `reference` opens

One surface per kind, and the frontend must not invent a second path to any of them.

| Kind | Opens |
|---|---|
| `item` | The item's popout — name, amount, recurrence, category, tags, note, sharing. |
| `person` | The person's popout — contact details, outstanding, settle-up. |
| `consumable` | The consumable's popout — quantity, per-use amount, purchase date. |
| `category` | The budget popout for that category. |
| `month` | The header's period retargets to that month. Not a popout — a navigation. |
| `goal` | The goal's projection popout. |
| `charge` | The untracked-charge popout, offering **Track this**. |
| `suggestion` | The rule-suggestion popout, offering **Accept**. |
| `slice` | Filters the siblings that asked for it (donut ↔ trend). |
| `period` | Retargets the header period. |

---

## 7. Keyboard

| Keys | Result |
|---|---|
| `Ctrl+K` | Command palette |
| `Ctrl+F` | Search, across all canvases |
| `Ctrl+E` | Toggle edit mode |
| `Ctrl+\` | Collapse / expand the rail |
| `↑ ↓ ← →` | Move the focused tile one cell (edit mode) |
| `Shift` + arrows | Resize the focused tile (edit mode) |
| `Tab` / `Shift+Tab` | Next / previous field. **A ledger row tabs straight into its amount.** |
| `Enter` | Activate: open the focused row, commit a field |
| `Escape` | Close the popout; else clear the filter; else exit edit mode. In that order. |
| `Delete` / `Backspace` | Delete the selected rows — **with a confirmation** |
| `Ctrl+Z` / `Ctrl+Shift+Z` | Undo / redo a grid edit |

Non-negotiables:

- **Focus is always visible.** Every custom-painted surface draws its own 2px `FOCUS` ring.
- **Every mouse action has a keyboard equivalent** (§31). A tile you can drag you can also
  move with arrows; a row you can click you can `Tab` to and `Enter`.
- **`Escape` unwinds one layer at a time.** Closing three things at once is a trap.

---

## 8. Key states

| State | Behaviour |
|---|---|
| **First run** | One canvas named **Dashboard**, seeded with the default Glance widgets, plus Settings. If there is no data, **one teaching block** — not fourteen empty tiles. |
| **No data at all** | Widgets hide (`is_empty`); the grid reflows; the teaching block explains what to import. |
| **One data point** | Show the number, suppress the series. A single point is a fact, not a trend. |
| **Resize below floor** | Snap back, 120ms ease-out. No dialog, no error, no explanation. |
| **Narrow window (<1100px)** | Grid drops to 6 columns and tiles step down a band. The 12-column geometry is **retained**, so widening restores the layout exactly. |
| **Last canvas** | Delete is disabled, with the reason in the menu item itself. |
| **Corrupt or missing layout** | Seed a default canvas and post one note. Never open blank. |
| **Sync failed** | An explicit *reconnect required* — never a silent "no new transactions". |
| **Store busy** | *Another window has it open.* Read-only for now; nothing is written and nothing is moved. |
| **Import partly parsed** | `412 read, 7 skipped`, counted and shown. Never silent. |
| **Money is empty** | `—`, never `$0.00`. A zero is a fact; an em dash is an absence. |

---

## 9. Accessibility

- **AA on every pairing**, verified against the tokens rather than by eye. `text-helper`,
  `text-error` and `link-primary` clear 4.5:1 on `background`, `layer-01` and `layer-02`.
  **On `layer-03` they do not** — keep that text off the third layer.
- **Sign is colour *and* a glyph.** `+$42.00` / `−$42.00`. No state is carried by hue alone,
  which is also why status is always a colour *and* a word.
- **Focus rings on every custom-painted surface** — exactly the places Qt will not draw one.
- **Hit targets are at least 32px** in any direction, even when the ink is smaller.
- **Nothing is mouse-only.** The palette and search are the keyboard path to everything.

---

## 10. Open, for whoever builds this

Decided above and not up for grabs: the grid geometry, the band table, the header row
contents, the reference kinds, the keyboard map, the content rules (sentence case, no
exclamation marks, numerals, plain errors that say what to do next).

Genuinely open:

1. **Where the Sheets page lives.** The previous frontend had it as a rail entry beside
   Settings. A redesign might put it in the palette only, or make it the third pane.
2. **How a filter is shown and cleared.** The header chip is a proposal; the requirement is
   only that a filtered canvas says so and can be unfiltered from one place.
3. **Whether the tray is a bar or a panel.** §27 says a bottom tray. Nothing depends on it.
4. **The exact empty states.** Each widget needs prose for "nothing here yet", and good
   prose is a writing job, not a layout one. The rule is only: explain, never blank.
