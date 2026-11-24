# GridFinance

A local-first personal budgeting backend: a recurring plan, a reconciled ledger, and the
machinery that connects them to statements, live feeds and an AI assistant.

**There is no frontend yet.** It was removed in 2026-10-08 to be redesigned; the backend,
its tests and the documents are what exist. Everything runs from Python today.
`FRONTEND.md` is the specification for the screen that replaces it.

- `BACKEND.md` — the contract: store, engine, hooks, and the AFM-derived features.
- `FRONTEND.md` — the interaction spec for the redesigned frontend.
- `design-doc.md` — the product specification both derive from.

## Requirements

- **Python 3.13 or newer.**
- **Windows** for credential encryption (DPAPI). On another platform, secrets are stored
  with a `plain:` prefix for development and are not protected.
- The core has **no third-party dependencies**. Optional extras add PDF parsing, the LLM
  SDKs and the MCP server.

## Install

```powershell
python -m pip install -e .
```

or, with every optional feature:

```powershell
python -m pip install -e ".[all]"
```

| Extra | Adds | Without it |
|---|---|---|
| `ingest` | `pdfplumber` | CSV, OFX and CAMT.053 still import; a PDF reports the missing extra |
| `ai` | `anthropic`, `openai` | no assistant or LLM categorisation; the reason is shown |
| `mcp` | `mcp` | the MCP server does not start; everything else works |
| `dev` | `pytest` | — |

## Test

```powershell
python -m pytest
```

The suite is Qt-free and needs no display. It currently runs 1,146 tests in a few seconds.

## Where data lives

Everything is under `data/` beside the checkout (beside the executable in a built app):

```
data/
  store.db                 the whole domain, in SQLite
  secrets.json             DPAPI-encrypted credentials
  backups/                 snapshots, including pre-migration copies
  statements/inbox/        drop statement files here
  statements/processed/    files already read
  exports/FY<year>/        generated markdown summaries
  profile.md               assistant notes about you
```

`store.db` and `secrets.json` belong together: copying the database to another machine
leaves the encrypted keys undecryptable, and the app reports them as unconfigured rather
than failing.

## Importing statements

Supported now: CSV, OFX, CAMT.053 and ANZ-style PDF (the last needs the `ingest` extra).
Drop files into `data/statements/inbox/` and scan:

```powershell
python -c "from finance_tool.store.store import Store; from finance_tool.engine.intake import scan_inbox; from finance_tool import paths; s = Store.load(paths.DATA_DIR); print([o.describe() for o in scan_inbox(s)]); s.close()"
```

Files are moved to `processed/` only after the store has been saved, and one statement is
one record in the import history even when it failed. Re-importing the same file imports
nothing. A statement that does not name its account is placed by masked number or unique
name, and refused if the answer is ambiguous.

## Running the integration surfaces

Both are optional and read the same `data/` directory.

```powershell
python -m finance_tool.mcp_server
```

```powershell
python -m finance_tool.httpapi
```

The HTTP API binds to `127.0.0.1:8000` and exposes read-only, CDR-shaped endpoints. It
compares a PIN held as a SHA-256 hash in `secrets.json`. **Without a PIN it serves
unauthenticated on the loopback interface** — fine for a quick local read, not for a shared
machine. Set one before starting it (avoid leaving the PIN in shell history):

```powershell
python -c "from finance_tool.httpapi import hash_pin; from finance_tool.store.secrets import load_secrets, save_secrets; s = load_secrets(); s.app_pin_hash = hash_pin(input('PIN: ')); save_secrets(s)"
```

## Migrating from AI Finance Manager

The migration is one-way, read-only on the AFM database, and verified before it reports
success. Start with a dry run:

```powershell
python scripts/migrate_afm.py --afm-db "..\AI Finance Manager\data\finance.db" --data-dir data --dry-run
```

Then run it without `--dry-run`. It refuses a destination that already holds data unless
you pass `--force`; force takes a backup of the database, secrets and profile first. Use
`--allow-live` only if you accept a snapshot of a database AFM still has open.

## Security notes

- **No banking write access.** There is no credential or code path that can move money;
  feeds are read-only and statements are files.
- **Statement data and keys stay on this machine**, except for requests you explicitly
  configure: the feed provider you choose, and the LLM provider you choose. Nothing else
  is uploaded.
- **Keys are DPAPI-encrypted** and never written to settings, exports or logs. A key copied
  from another Windows account reads as absent.
- **The assistant can write only when allowed.** `Settings.ai_writes` gates every mutating
  tool, and a `before-ai` revision is taken before the first write.
- **The HTTP API never writes** and binds to loopback only.
