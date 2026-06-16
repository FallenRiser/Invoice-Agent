"""Minimal live dashboard over the agent's own review queue + audit log.

Answers "where do humans see what needs review" -- deliberately NOT inside Odoo, because the
whole point of the validation pipeline is that anything incomplete/unapproved/suspicious
never touches account.move in the first place. This page reads/writes the
exact same sqlite db and calls the exact same pipeline functions as the agent.

Run with `python scripts/run_dashboard.py`, then open http://127.0.0.1:8001.
"""
from __future__ import annotations

import logging
from datetime import datetime
from html import escape
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.config import append_supplier_override, get_settings
from app.observability import audit_log
from app.repositories import invoice_repo, review_repo
from app.repositories.db import init_db
from app.schemas.invoice import ExtractedInvoice
from app.schemas.review import Decision, extracted_from_json
from app.services import odoo_service, validation_service
from app.services.math_validation import validate_invoice_math

logger = logging.getLogger(__name__)

app = FastAPI(title="Invoice Agent - Review Dashboard")

_DECISION_COLORS = {
    "POSTED": "#1a7f37",
    "NO_ACTION_NEEDED": "#6e7781",
    "NEW_VENDOR_SE": "#9a6700",
    "DUPLICATE": "#9a6700",
    "INCOMPLETE": "#cf222e",
    "EXTRACTION_FAILED": "#cf222e",
    "MANUALLY_RESOLVED": "#57606a",
    "MATH_MISMATCH": "#cf222e",
    "ODOO_ERROR": "#cf222e",
}

def _badge(decision: str) -> str:
    color = _DECISION_COLORS.get(decision, "#444444")
    return (
        f'<span style="background:{color}1a;color:{color};border:1px solid {color}55;'
        f'border-radius:4px;padding:2px 8px;font-size:12px;font-weight:600;white-space:nowrap;">'
        f"{escape(decision)}</span>"
    )

def _vendor_name(extracted_json: str | None) -> str:
    invoice = extracted_from_json(extracted_json)
    if invoice and invoice.vendor_name:
        return invoice.vendor_name
    return "-"

def _review_row(row: dict) -> str:
    vendor = escape(_vendor_name(row.get("extracted_json")))
    handled_btn = (
        f'<form method="post" action="/resolve/{row["id"]}" style="display:inline">'
        f'<button type="submit">Mark handled</button></form>'
    )
    if row["decision"] == "NEW_VENDOR_SE":
        approve_btn = (
            f'<form method="post" action="/approve/{row["id"]}" style="display:inline">'
            f'<button type="submit" style="background:#1a7f37;color:white;border:none;'
            f'border-radius:4px;padding:4px 10px;cursor:pointer;">Approve vendor &amp; re-run</button>'
            f"</form> "
        )
        actions = approve_btn + handled_btn
    else:
        actions = handled_btn
    return (
        "<tr>"
        f"<td>{escape(row['source_file'])}</td>"
        f"<td>{vendor}</td>"
        f"<td>{_badge(row['decision'])}</td>"
        f"<td>{escape(row['reason'])}</td>"
        f"<td>{escape(str(row['created_at']))}</td>"
        f"<td>{actions}</td>"
        "</tr>"
    )

def _audit_row(row: dict) -> str:
    return (
        "<tr>"
        f"<td>{escape(str(row['created_at']))}</td>"
        f"<td>{escape(row['source_file'])}</td>"
        f"<td>{_badge(row['decision'])}</td>"
        f"<td>{escape(row['reason'])}</td>"
        f"<td>{escape(row.get('detail') or '')}</td>"
        "</tr>"
    )

_PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Invoice Agent - Review Dashboard</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; margin: 32px;
          background: #f6f8fa; color: #1f2328; }}
  h1 {{ font-size: 20px; margin: 0; }}
  h2 {{ font-size: 15px; margin-top: 36px; color: #57606a; }}
  table {{ width: 100%; border-collapse: collapse; background: white; border: 1px solid #d0d7de;
           border-radius: 6px; overflow: hidden; }}
  th, td {{ text-align: left; padding: 8px 12px; border-bottom: 1px solid #eaeef2; font-size: 13px;
            vertical-align: middle; }}
  th {{ background: #f6f8fa; font-weight: 600; }}
  tr:last-child td {{ border-bottom: none; }}
  button {{ font-size: 12px; padding: 4px 8px; border: 1px solid #d0d7de; border-radius: 4px;
            background: white; cursor: pointer; }}
  button:hover {{ opacity: 0.85; }}
  .empty {{ color: #6e7781; font-style: italic; padding: 12px; background: white;
            border: 1px solid #d0d7de; border-radius: 6px; }}
  .topbar {{ display: flex; justify-content: space-between; align-items: baseline; }}
  .meta {{ color: #6e7781; font-size: 12px; }}
  .run-btn {{ background: #0969da; color: white; border: none; padding: 6px 14px;
              border-radius: 4px; cursor: pointer; font-size: 13px; }}
</style>
</head>
<body>
  <div class="topbar">
    <h1>Invoice Agent - Review Dashboard</h1>
    <form method="post" action="/run-pipeline">
      <button type="submit" class="run-btn">Check inbox now</button>
    </form>
  </div>
  <p class="meta">Refreshed {now} * {pending_count} item(s) awaiting review</p>

  <h2>Needs human review</h2>
  {review_table}

  <h2>Recent activity (audit log)</h2>
  {audit_table}
</body>
</html>"""

def _render() -> str:
    init_db()
    pending = review_repo.list_pending()
    audit_rows = list(reversed(audit_log.list_all()))[:40]

    if pending:
        review_table = (
            "<table><tr><th>File</th><th>Vendor</th><th>Decision</th><th>Reason</th>"
            "<th>Flagged</th><th>Action</th></tr>"
            + "".join(_review_row(r) for r in pending)
            + "</table>"
        )
    else:
        review_table = (
            '<div class="empty">Nothing pending - everything either posted '
            "automatically or has already been resolved.</div>"
        )

    if audit_rows:
        audit_table = (
            "<table><tr><th>When</th><th>File</th><th>Decision</th><th>Reason</th>"
            "<th>Detail</th></tr>"
            + "".join(_audit_row(r) for r in audit_rows)
            + "</table>"
        )
    else:
        audit_table = '<div class="empty">No activity logged yet - drop a file in data/inbox to test.</div>'

    return _PAGE.format(
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        pending_count=len(pending),
        review_table=review_table,
        audit_table=audit_table,
    )

def revalidate_pending() -> None:
    """Re-run validation on every still-PENDING review item."""
    for row in review_repo.list_pending():
        invoice = extracted_from_json(row.get("extracted_json"))
        if not invoice:
            continue
        
        math_result = validate_invoice_math(invoice, filename=row["source_file"])
        if math_result.has_mismatch:
            continue
            
        outcome = validation_service.validate(invoice)
        if outcome.decision != Decision.POSTED:
            continue
            
        move_id = odoo_service.post_invoice(invoice, source_file=row["source_file"])
        invoice_repo.record_processed(outcome.normalized_vendor, invoice.invoice_number, row["source_file"], move_id)
        review_repo.resolve(row["id"])
        audit_log.write(
            row["source_file"], Decision.POSTED.value,
            "Posted on re-validation (override/approval added since last run).",
            detail=f"odoo_move_id={move_id}",
        )

def trigger_watcher_sweep() -> None:
    """Trigger the running inbox watcher to process existing unflagged PDFs."""
    settings = get_settings()
    inbox_dir = settings.paths.inbox
    if not inbox_dir.exists():
        return
        
    pending = review_repo.list_pending()
    pending_files = {r["source_file"] for r in pending}
    
    for pdf in inbox_dir.glob("*.pdf"):
        if pdf.name not in pending_files:
            tmp_path = pdf.with_suffix(".pdf.tmp")
            try:
                pdf.rename(tmp_path)
                tmp_path.rename(pdf)
            except Exception:
                pass

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(_render())

@app.post("/run-pipeline")
def trigger_run() -> RedirectResponse:
    trigger_watcher_sweep()
    return RedirectResponse("/", status_code=303)

@app.post("/approve/{review_id}")
def approve(review_id: int) -> RedirectResponse:
    row = next((r for r in review_repo.list_pending() if r["id"] == review_id), None)
    if row:
        vendor = _vendor_name(row.get("extracted_json"))
        if vendor and vendor != "-":
            append_supplier_override(vendor, approved_by="Dashboard (SE review)")
        revalidate_pending()
    return RedirectResponse("/", status_code=303)

@app.post("/resolve/{review_id}")
def resolve(review_id: int) -> RedirectResponse:
    row = next((r for r in review_repo.list_pending() if r["id"] == review_id), None)
    review_repo.resolve(review_id)
    if row:
        audit_log.write(
            row["source_file"],
            "MANUALLY_RESOLVED",
            "Marked handled via dashboard (no automated re-validation path for this decision).",
        )
    return RedirectResponse("/", status_code=303)
