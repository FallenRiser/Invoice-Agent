"""
End-to-end verification harness.

Drives the REAL pipeline (math validation -> validation_service -> odoo_service
(mock) -> review_repo / invoice_repo / audit_log -> file move) against the 8
sample invoices in data/inbox/, using hand-built ExtractedInvoice objects that
mirror exactly what a correctly-functioning LLM extraction step would produce
from each PDF's actual text (read directly from the PDFs via pdfplumber --
no guessing). The only thing this script substitutes for is the LLM call
itself (no Ollama server is reachable in this environment); every other
module -- math_validation, validation_service, vendor_match, supplier_repo,
the SQLite repos, audit_log, and the mock Odoo client -- runs unmodified.

This mirrors app/watchers/inbox_watcher.py's InboxEventHandler._process()
step-for-step (steps 6-9), so a pass here is a real proof the wiring behaves
as specified, not just a unit test of one function in isolation.

Run with: python3 scripts/verify_pipeline.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.observability import audit_log
from app.repositories import invoice_repo, review_repo
import app.repositories.db as db_module
from app.repositories.db import get_conn, init_db
from app.schemas.invoice import ExtractedInvoice, LineItem
from app.schemas.review import Decision
from app.services import odoo_service, validation_service
from app.services.math_validation import validate_invoice_math
from app.core.config import get_settings

# This verification sandbox mounts the project folder over a FUSE/bindfs
# bridge that doesn't support the file locking SQLite's rollback journal
# needs -- writes intermittently raise "disk I/O error" even under
# journal_mode=MEMORY. Point the verification run's DB at a plain local path
# instead, so the logic under test (repos, validation, audit log) gets a
# real working connection. This has zero bearing on the shipped app: once
# this runs on the user's own machine, data/agent.db sits on a normal
# filesystem and needs no such override.
_VERIFY_DB_PATH = Path("/tmp/huma_verify_agent.db")
if _VERIFY_DB_PATH.exists():
    _VERIFY_DB_PATH.unlink()
db_module.get_db_path = lambda: _VERIFY_DB_PATH


def li(description, quantity, unit_price, amount):
    return LineItem(description=description, quantity=quantity, unit_price=unit_price, amount=amount)


# ── Hand-built ExtractedInvoice for each sample, transcribed directly from
# the PDF text extracted via pdfplumber (see verification log for the raw
# text). Each represents what correct LLM extraction should produce. ────────

SAMPLES: list[tuple[str, ExtractedInvoice, str]] = [
    ("01_AWS.pdf", ExtractedInvoice(
        vendor_name="Amazon Web Services EMEA SARL",
        invoice_number="EUINGB26-2041785",
        invoice_date="2026-04-30",
        due_date=None,
        total_amount=3072.24,
        currency="USD",
        payment_terms="Charged to card ending 4421",
        tax_type="VAT (reverse charge)",
        tax_amount=0.00,
        document_type="invoice",
        extraction_confidence=0.97,
        line_items=[
            li("Amazon EC2 — compute (April 2026)", 1, 1842.66, 1842.66),
            li("Amazon S3 — storage & requests", 1, 612.40, 612.40),
            li("Amazon CloudFront — data transfer", 1, 327.18, 327.18),
            li("AWS Support (Business)", 1, 290.00, 290.00),
        ],
    ), "POSTED (exact vendor match, math verified, immediate-payment so no due_date needed)"),

    ("02_GoogleCloud.pdf", ExtractedInvoice(
        vendor_name="Google Cloud EMEA Limited",
        invoice_number="3920184473",
        invoice_date="2026-04-30",
        due_date="2026-05-30",
        total_amount=3813.80,
        currency="EUR",
        payment_terms="Net 30",
        tax_type="VAT (reverse charge)",
        tax_amount=0.00,
        document_type="invoice",
        extraction_confidence=0.97,
        line_items=[
            li("Compute Engine — n2 instances", 1, 2410.55, 2410.55),
            li("BigQuery — analysis & storage", 1, 988.20, 988.20),
            li("Cloud Networking — egress", 1, 415.05, 415.05),
        ],
    ), "POSTED (exact vendor match, math verified)"),

    ("03_Atlassian.pdf", ExtractedInvoice(
        vendor_name="Atlassian Pty Ltd",
        invoice_number="AT-558210",
        invoice_date="2026-04-17",
        due_date="2026-05-17",
        total_amount=6875.00,
        currency="USD",
        payment_terms="Net 30",
        tax_type="Tax",
        tax_amount=0.00,
        document_type="invoice",
        extraction_confidence=0.96,
        line_items=[
            li("Jira Software — Standard (50 users), annual", 1, 3850.00, 3850.00),
            li("Confluence — Standard (50 users), annual", 1, 3025.00, 3025.00),
        ],
    ), "POSTED (exact vendor match, math verified) -- processed BEFORE its resend below"),

    ("04_Sentry.pdf", ExtractedInvoice(
        vendor_name="Functional Software, Inc. (Sentry)",
        invoice_number="2026-3391",
        invoice_date="2026-04-12",
        due_date=None,
        total_amount=312.00,
        currency="USD",
        payment_terms="Paid — Visa ending 4421",
        tax_type="Sales tax",
        tax_amount=0.00,
        document_type="receipt",
        extraction_confidence=0.95,
        line_items=[
            li("Sentry — Team plan, monthly subscription", 1, 312.00, 312.00),
        ],
    ), "POSTED (exact vendor match incl. brand parenthetical, math verified, 'Paid' exempts due_date)"),

    ("05_Communere.pdf", ExtractedInvoice(
        vendor_name="Communere Ltd",
        invoice_number="CMN-0091",
        invoice_date="2026-05-01",
        due_date="2026-05-15",
        total_amount=6408.00,
        currency="GBP",
        payment_terms="Net 14",
        tax_type="VAT (20%)",
        tax_amount=1068.00,
        document_type="invoice",
        extraction_confidence=0.96,
        line_items=[
            li("Clinical content licensing — Q2 2026", 1, 4200.00, 4200.00),
            li("Editorial & medical review services", 12, 95.00, 1140.00),
        ],
    ), "POSTED (exact vendor match, math verified incl. tax)"),

    ("06_Northwind.pdf", ExtractedInvoice(
        vendor_name="Northwind Office Supplies Ltd",
        invoice_number="7741",
        invoice_date="2026-04-28",
        due_date="2026-05-28",
        total_amount=2199.00,
        currency="GBP",
        payment_terms="Net 30",
        tax_type="VAT (20%)",
        tax_amount=366.50,
        document_type="invoice",
        extraction_confidence=0.95,
        line_items=[
            li("Ergonomic office chair", 4, 189.00, 756.00),
            li("Sit-stand desk (1600mm)", 2, 415.00, 830.00),
            li("Stationery & supplies bundle", 1, 246.50, 246.50),
        ],
    ), "NEW_VENDOR_SE (vendor not on Approved Supplier List -- math is fine, but never reached)"),

    ("07_Atlassian_resend.pdf", ExtractedInvoice(
        vendor_name="Atlassian Pty Ltd",
        invoice_number="AT-558210",  # identical to 03 -- this IS the resend
        invoice_date="2026-04-17",
        due_date="2026-05-17",
        total_amount=6875.00,
        currency="USD",
        payment_terms="Net 30",
        tax_type="Tax",
        tax_amount=0.00,
        document_type="invoice",
        extraction_confidence=0.96,
        line_items=[
            li("Jira Software — Standard (50 users), annual", 1, 3850.00, 3850.00),
            li("Confluence — Standard (50 users), annual", 1, 3025.00, 3025.00),
        ],
    ), "DUPLICATE (same vendor + invoice number as 03, already recorded as processed)"),

    ("08_GitHub_missing_number.pdf", ExtractedInvoice(
        vendor_name="GitHub, Inc.",
        invoice_number=None,  # genuinely absent from the document
        invoice_date="2026-05-03",
        due_date="2026-06-02",
        total_amount=6555.00,
        currency="USD",
        payment_terms="Net 30",
        tax_type="Tax",
        tax_amount=0.00,
        document_type="invoice",
        extraction_confidence=0.93,
        line_items=[
            li("GitHub Enterprise — 25 seats, annual", 1, 5250.00, 5250.00),
            li("GitHub Advanced Security — 25 committers", 1, 1305.00, 1305.00),
        ],
    ), "INCOMPLETE (invoice_number missing -- vendor IS on the approved list, never reached)"),
]


def math_summary(math_result) -> str:
    if math_result.is_fully_verified:
        return "verified"
    if math_result.has_mismatch:
        return "MISMATCH"
    return "incomplete"


def process(filename: str, invoice: ExtractedInvoice, inbox_dir: Path, processed_dir: Path) -> str:
    """Mirrors InboxEventHandler._process() steps 6-9 exactly."""
    path = inbox_dir / filename

    math_result = validate_invoice_math(invoice, filename=filename)
    outcome = validation_service.validate(invoice)
    print(f"  [VALIDATE] {filename} -> {outcome.decision.value} ({outcome.reason})")
    print(f"  [MATH]     {filename} -> {math_summary(math_result)}")

    if outcome.decision != Decision.POSTED:
        from app.schemas.review import extracted_to_json
        flag_id = review_repo.add_flag(filename, outcome.decision.value, outcome.reason, extracted_to_json(invoice))
        audit_log.write(filename, outcome.decision.value, outcome.reason, detail=f"review_queue_id={flag_id}")
        print(f"  [REVIEW]   flagged (review_queue id={flag_id}) -- stays in inbox/")
        return outcome.decision.value

    if math_result.has_mismatch:
        from app.schemas.review import extracted_to_json
        reason = "Arithmetic does not reconcile."
        flag_id = review_repo.add_flag(filename, Decision.MATH_MISMATCH.value, reason, extracted_to_json(invoice))
        audit_log.write(filename, Decision.MATH_MISMATCH.value, reason, detail=f"review_queue_id={flag_id}")
        print(f"  [REVIEW]   flagged MATH_MISMATCH (review_queue id={flag_id}) -- stays in inbox/")
        return Decision.MATH_MISMATCH.value

    try:
        move_id = odoo_service.post_invoice(invoice, source_file=filename)
    except Exception as exc:
        from app.schemas.review import extracted_to_json
        reason = f"Odoo posting failed: {exc}"
        flag_id = review_repo.add_flag(filename, Decision.ODOO_ERROR.value, reason, extracted_to_json(invoice))
        audit_log.write(filename, Decision.ODOO_ERROR.value, reason, detail=f"review_queue_id={flag_id}")
        print(f"  [REVIEW]   flagged ODOO_ERROR (review_queue id={flag_id}) -- stays in inbox/")
        return Decision.ODOO_ERROR.value

    invoice_repo.record_processed(outcome.normalized_vendor, invoice.invoice_number, filename, move_id)
    audit_log.write(filename, Decision.POSTED.value, outcome.reason, detail=f"odoo_move_id={move_id}")

    import shutil
    processed_dir.mkdir(parents=True, exist_ok=True)
    dest = processed_dir / filename
    if path.exists():
        shutil.move(str(path), str(dest))
        print(f"  [MOVE]     '{filename}' -> processed/ (odoo move #{move_id})")
    else:
        print(f"  [MOVE]     WARNING: '{filename}' not found in inbox/ to move (already moved?)")
    return Decision.POSTED.value


def main() -> int:
    settings = get_settings()
    inbox_dir = settings.paths.inbox
    processed_dir = settings.paths.processed
    init_db()

    print("=" * 78)
    print("PIPELINE VERIFICATION -- 8 sample invoices")
    print("=" * 78)

    results = []
    failures = []
    for filename, invoice, expected in SAMPLES:
        print(f"\n--- {filename} ---")
        print(f"  expected: {expected}")
        actual = process(filename, invoice, inbox_dir, processed_dir)
        expected_decision = expected.split(" ", 1)[0]
        ok = actual == expected_decision
        results.append((filename, expected_decision, actual, ok))
        if not ok:
            failures.append(filename)

    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    for filename, expected_decision, actual, ok in results:
        flag = "OK  " if ok else "FAIL"
        print(f"  [{flag}] {filename:32s} expected={expected_decision:12s} actual={actual}")

    print("\n--- data/inbox/ contents after run ---")
    for p in sorted(inbox_dir.glob("*.pdf")):
        print(f"  {p.name}")

    print("\n--- data/processed/ contents after run ---")
    for p in sorted(processed_dir.glob("*.pdf")):
        print(f"  {p.name}")

    print("\n--- review_queue rows ---")
    for row in review_repo.list_pending():
        print(f"  {dict(row) if hasattr(row, 'keys') else row}")

    print("\n--- processed_invoices rows ---")
    with get_conn() as conn:
        for row in conn.execute("SELECT * FROM processed_invoices"):
            print(f"  {dict(row)}")

    print("\n--- audit_log rows ---")
    with get_conn() as conn:
        for row in conn.execute("SELECT * FROM audit_log ORDER BY id"):
            print(f"  {dict(row)}")

    print("\n" + "=" * 78)
    if failures:
        print(f"RESULT: {len(failures)} FAILURE(S): {failures}")
        return 1
    print("RESULT: ALL 8 SAMPLES MATCHED EXPECTED DECISIONS")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
