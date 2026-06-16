"""
The compliance checks from the brief, run in a fixed order so the *first*
applicable reason is what gets surfaced to a human:

  1. Completeness       -- every mandatory field present (and each line item
                            has enough detail to be math-checked).
  2. Approved supplier   -- vendor must be on the Approved Supplier List,
                            confidently matched, to auto-post.
  3. Duplicate detection -- same vendor + invoice number already processed.
  4. Otherwise           -- POSTED (caller still applies the math check on
                            top of this before actually posting -- see
                            app/watchers/inbox_watcher.py).
"""
from __future__ import annotations

from app.core.config import get_settings
from app.repositories import invoice_repo
from app.repositories.supplier_repo import load_approved_suppliers
from app.schemas.invoice import ExtractedInvoice
from app.schemas.review import Decision, ValidationOutcome
from app.utils.vendor_match import match_against_approved, normalize_vendor_name


def _is_immediate_payment(payment_terms: str | None) -> bool:
    if not payment_terms:
        return False
    markers = get_settings().completeness.immediate_payment_terms_markers
    text = payment_terms.lower()
    return any(m.lower() in text for m in markers)


def check_completeness(invoice: ExtractedInvoice) -> list[str]:
    """Returns the list of problems found (empty list = complete)."""
    mandatory = get_settings().completeness.mandatory_fields
    missing: list[str] = []

    for field_name in mandatory:
        value = getattr(invoice, field_name, None)
        empty = value is None or value == "" or value == []
        if not empty:
            continue
        if field_name == "due_date" and _is_immediate_payment(invoice.payment_terms):
            continue  # genuinely not applicable -- auto-charged/already-paid documents
        missing.append(field_name)

    # Line items being present (checked above) isn't enough on its own -- a line
    # item missing quantity/unit_price can't be math-checked or trusted either.
    if invoice.line_items and "line_items" not in missing:
        for i, item in enumerate(invoice.line_items, start=1):
            if item.quantity is None or item.unit_price is None:
                missing.append(f"line_items[{i}].quantity/unit_price")

    return missing


def validate(invoice: ExtractedInvoice) -> ValidationOutcome:
    # 1. Completeness
    missing = check_completeness(invoice)
    if missing:
        return ValidationOutcome(
            decision=Decision.INCOMPLETE,
            reason=f"Missing or incomplete mandatory field(s): {', '.join(missing)}",
            missing_fields=missing,
            normalized_vendor=normalize_vendor_name(invoice.vendor_name or ""),
            matched_supplier_name=None,
        )

    normalized_vendor = normalize_vendor_name(invoice.vendor_name)

    # 2. Approved-supplier check (name matching)
    approved = load_approved_suppliers()
    match = match_against_approved(invoice.vendor_name, approved)
    if not match.matched:
        return ValidationOutcome(
            decision=Decision.NEW_VENDOR_SE,
            reason=f"'{invoice.vendor_name}' is not on the Approved Supplier List -- route to Supplier Evaluation.",
            missing_fields=[],
            normalized_vendor=normalized_vendor,
            matched_supplier_name=None,
        )
    if not match.confident:
        return ValidationOutcome(
            decision=Decision.NEW_VENDOR_SE,
            reason=(
                f"'{invoice.vendor_name}' only fuzzy-matched '{match.supplier_name}' "
                f"(score {match.score:.0f}) -- not an exact match, needs human confirmation."
            ),
            missing_fields=[],
            normalized_vendor=normalized_vendor,
            matched_supplier_name=match.supplier_name,
        )

    # 3. Duplicate detection
    if invoice_repo.is_duplicate(normalized_vendor, invoice.invoice_number):
        return ValidationOutcome(
            decision=Decision.DUPLICATE,
            reason=f"Invoice number '{invoice.invoice_number}' already processed for this vendor.",
            missing_fields=[],
            normalized_vendor=normalized_vendor,
            matched_supplier_name=match.supplier_name,
        )

    # 4. All checks passed (math is verified separately, on top of this, by the caller)
    return ValidationOutcome(
        decision=Decision.POSTED,
        reason="All checks passed.",
        missing_fields=[],
        normalized_vendor=normalized_vendor,
        matched_supplier_name=match.supplier_name,
    )
