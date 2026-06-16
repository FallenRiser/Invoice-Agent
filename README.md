# Automated Odoo Invoicing Workflow Agent

Take-home submission for Huma's Human Agent role. Monitors an inbox for vendor PDF
invoices, extracts structured fields with a local LLM, validates against business rules
(including math checks), and pushes valid bills into Odoo as Vendor Bills — flagging anything else for human review.

**Start here:**
- [`DESIGN.md`](./DESIGN.md) — architecture, assumptions, edge cases, roadmap, and the call walkthrough script.
- [`ODOO_SETUP.md`](./ODOO_SETUP.md) — stand up the live Odoo instance (Podman) used for the demo.

## What's real vs. mocked

| Stage | Status |
|---|---|
| Inbox monitoring | Mocked — continuous watched folder (`data/inbox/`) stands in for Gmail. Watchdog events automatically trigger processing. |
| Extraction | Real — local LLM via Ollama + LangChain `ChatOllama` (no cloud key used). |
| Validation | Real — completeness, approved-supplier, duplicate, math validation, already-paid/receipt checks. |
| Odoo push | Real — self-hosted Odoo (Podman), actual `account.move` records over XML-RPC, chatter audit notes. |
| Audit log | Real — local SQLite ledger. |

Full reasoning for each call in [`DESIGN.md` §1–2](./DESIGN.md).

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
```

1. Stand up Odoo (see `ODOO_SETUP.md`), then `cp .env.example .env` and fill in
   `ODOO_USERNAME` / `ODOO_PASSWORD`.
2. Pull a local model: `ollama pull gemma3:4b` (or `qwen3:4b`) and make sure
   `ollama serve` is running.
3. Run the agent (it will run continuously and watch the inbox):

```bash
python scripts/run_agent.py
```

Drop PDFs into `data/inbox/` to see them processed instantly. The agent uses Python's `watchdog` to respond to file system events.

## Review dashboard

Flagged invoices (incomplete, unapproved vendor, duplicate, math mismatch) deliberately never touch Odoo —
so there's nothing to review inside Odoo itself. The console output covers that, but there's also a small live dashboard over the same `data/agent.db` and the same pipeline functions (no separate state):

```bash
python scripts/run_dashboard.py   # http://127.0.0.1:8001
```

Shows what's pending, lets you approve a new vendor with one click (writes to
`config/approved_suppliers_overrides.yaml` and immediately re-validates/posts), mark anything else as manually handled, and manually trigger a
fresh inbox sweep for old files. Entirely optional — the CLI + sqlite db is the source of truth either way.

## Configuration

Everything that should change without a code change lives in `config/*.yaml` + `.env` —
see `DESIGN.md` §7. In short:

- `config/required_fields.yaml` — the full extraction schema. Single source of truth.
- `config/approved_suppliers_overrides.yaml` — vendors approved after the original xlsx, additive.
- `config/settings.yaml` — fuzzy-match threshold, receipt/already-paid markers, currency symbols.
- `.env` — LLM provider/model and Odoo connection secrets.
