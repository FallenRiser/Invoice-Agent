"""
Deterministic arithmetic validation for extracted invoices.

The LLM extraction step (app/integration/llm/ollama_extractor.py) gets every
field handed to it from raw PDF text -- it can misread a digit, transpose a
decimal, or simply report a total that doesn't actually add up. None of that
is caught by JSON-parsing leniency: the JSON can be perfectly well-formed and
still be wrong. This module re-derives the numbers with plain arithmetic --
no LLM involved -- and checks them against what the model reported.

Two checks, both run independently and both logged in full regardless of
outcome:

  1. Per line item:    quantity * unit_price  ≈  stated amount
  2. Invoice total:     sum(line item amounts) + tax_amount  ≈  total_amount

A check only runs when every input it needs is present. A missing field
is logged and reported as "skipped" -- never silently treated as a pass
*or* a failure. (Whether a missing field should block posting is the
completeness checker's job, not this one's.)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from app.core.config import get_settings
from app.schemas.invoice import ExtractedInvoice, LineItem

logger = logging.getLogger(__name__)


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class LineItemCheck:
    index:           int              # 1-based, for human-readable logs
    description:     Optional[str]
    quantity:        Optional[float]
    unit_price:      Optional[float]
    stated_amount:   Optional[float]
    computed_amount: Optional[float]  # quantity * unit_price, if both present
    delta:           Optional[float]  # stated_amount - computed_amount
    status:          str              # "match" | "mismatch" | "skipped"
    note:            str = ""


@dataclass
class TotalsCheck:
    line_item_sum:  Optional[float]   # sum of stated line item amounts
    tax_amount:     Optional[float]
    expected_total: Optional[float]   # line_item_sum + tax_amount
    stated_total:   Optional[float]   # invoice.total_amount
    delta:          Optional[float]   # stated_total - expected_total
    status:         str               # "match" | "mismatch" | "skipped"
    note:           str = ""


@dataclass
class MathValidationResult:
    line_item_checks: list[LineItemCheck] = field(default_factory=list)
    totals_check:      Optional[TotalsCheck] = None

    @property
    def has_mismatch(self) -> bool:
        if any(c.status == "mismatch" for c in self.line_item_checks):
            return True
        if self.totals_check is not None and self.totals_check.status == "mismatch":
            return True
        return False

    @property
    def is_fully_verified(self) -> bool:
        """True only if every check that could run, ran, and passed."""
        if any(c.status != "match" for c in self.line_item_checks):
            return False
        if self.totals_check is None or self.totals_check.status != "match":
            return False
        return True


# ── Internal helpers ──────────────────────────────────────────────────────────

def _within_tolerance(a: float, b: float, tolerance: float) -> bool:
    # +1e-9 absorbs IEEE-754 representation noise (e.g. 100.0 - 99.99 comes
    # out to ~0.010000000000005 in float64) so a genuinely-exact cent-level
    # match at the tolerance boundary doesn't flip to "mismatch" by a
    # fraction of a billionth of a cent.
    return abs(a - b) <= tolerance + 1e-9


def _check_line_item(index: int, item: LineItem, tolerance: float) -> LineItemCheck:
    quantity, unit_price, stated_amount = item.quantity, item.unit_price, item.amount

    if quantity is None or unit_price is None:
        return LineItemCheck(
            index=index, description=item.description,
            quantity=quantity, unit_price=unit_price, stated_amount=stated_amount,
            computed_amount=None, delta=None, status="skipped",
            note="missing quantity and/or unit_price -- cannot recompute",
        )

    computed_amount = round(quantity * unit_price, 2)

    if stated_amount is None:
        return LineItemCheck(
            index=index, description=item.description,
            quantity=quantity, unit_price=unit_price, stated_amount=None,
            computed_amount=computed_amount, delta=None, status="skipped",
            note="LLM did not report an amount for this line to compare against",
        )

    delta = round(stated_amount - computed_amount, 2)
    status = "match" if _within_tolerance(stated_amount, computed_amount, tolerance) else "mismatch"
    return LineItemCheck(
        index=index, description=item.description,
        quantity=quantity, unit_price=unit_price, stated_amount=stated_amount,
        computed_amount=computed_amount, delta=delta, status=status,
    )


def _check_totals(
    invoice: ExtractedInvoice,
    line_item_checks: list[LineItemCheck],
    tolerance: float,
) -> TotalsCheck:
    if not invoice.line_items:
        return TotalsCheck(
            line_item_sum=None, tax_amount=invoice.tax_amount,
            expected_total=None, stated_total=invoice.total_amount,
            delta=None, status="skipped", note="no line items were extracted",
        )

    stated_amounts = [c.stated_amount for c in line_item_checks if c.stated_amount is not None]
    if len(stated_amounts) != len(line_item_checks):
        return TotalsCheck(
            line_item_sum=None, tax_amount=invoice.tax_amount,
            expected_total=None, stated_total=invoice.total_amount,
            delta=None, status="skipped",
            note="one or more line items has no stated amount -- sum would be incomplete",
        )

    line_item_sum = round(sum(stated_amounts), 2)
    tax_amount = invoice.tax_amount or 0.0
    expected_total = round(line_item_sum + tax_amount, 2)

    if invoice.total_amount is None:
        return TotalsCheck(
            line_item_sum=line_item_sum, tax_amount=invoice.tax_amount,
            expected_total=expected_total, stated_total=None,
            delta=None, status="skipped", note="LLM did not report a total_amount",
        )

    delta = round(invoice.total_amount - expected_total, 2)
    # Per-line rounding compounds across items -- scale the tolerance accordingly.
    scaled_tolerance = tolerance * max(1, len(line_item_checks))
    status = "match" if _within_tolerance(invoice.total_amount, expected_total, scaled_tolerance) else "mismatch"
    return TotalsCheck(
        line_item_sum=line_item_sum, tax_amount=invoice.tax_amount,
        expected_total=expected_total, stated_total=invoice.total_amount,
        delta=delta, status=status,
    )


def _log_result(filename: str, result: MathValidationResult) -> None:
    label = f"'{filename}'" if filename else "input"
    logger.info("[MATH]  Deterministic number validation for %s", label)

    if not result.line_item_checks:
        logger.info("[MATH]    No line items to check.")

    for c in result.line_item_checks:
        if c.status == "skipped":
            logger.warning(
                "[MATH]    Line %d  SKIPPED   (%s)  qty=%s unit_price=%s stated_amount=%s -- %s",
                c.index, c.description, c.quantity, c.unit_price, c.stated_amount, c.note,
            )
        elif c.status == "match":
            logger.info(
                "[MATH]    Line %d  MATCH     (%s)  %s x %s = %s (stated %s, delta %s)",
                c.index, c.description, c.quantity, c.unit_price,
                c.computed_amount, c.stated_amount, c.delta,
            )
        else:
            logger.warning(
                "[MATH]    Line %d  MISMATCH  (%s)  %s x %s = %s but LLM stated %s (delta %s)",
                c.index, c.description, c.quantity, c.unit_price,
                c.computed_amount, c.stated_amount, c.delta,
            )

    t = result.totals_check
    if t is not None:
        if t.status == "skipped":
            logger.warning("[MATH]    Totals    SKIPPED   -- %s", t.note)
        elif t.status == "match":
            logger.info(
                "[MATH]    Totals    MATCH     line_items(%s) + tax(%s) = %s (stated total %s, delta %s)",
                t.line_item_sum, t.tax_amount, t.expected_total, t.stated_total, t.delta,
            )
        else:
            logger.warning(
                "[MATH]    Totals    MISMATCH  line_items(%s) + tax(%s) = %s but LLM stated total %s (delta %s)",
                t.line_item_sum, t.tax_amount, t.expected_total, t.stated_total, t.delta,
            )

    if result.is_fully_verified:
        summary = "ALL CHECKS PASSED"
    elif result.has_mismatch:
        summary = "MISMATCH DETECTED"
    else:
        summary = "INCOMPLETE -- one or more checks skipped"
    logger.info("[MATH]  Result for %s: %s", label, summary)


# ── Public API ────────────────────────────────────────────────────────────────

def validate_invoice_math(invoice: ExtractedInvoice, filename: str = "") -> MathValidationResult:
    """
    Re-derive line item and total amounts with plain arithmetic and compare
    them against what the LLM reported. Fully independent of the model --
    this catches extraction errors that are valid JSON but wrong numbers.

    Always logs every check it runs (and every check it skips, with why).
    Returns the structured result so callers can act on
    `result.has_mismatch` / `result.is_fully_verified` later (e.g. routing
    to a review queue once that layer exists).
    """
    tolerance = get_settings().validation.amount_tolerance

    line_item_checks = [
        _check_line_item(i, item, tolerance)
        for i, item in enumerate(invoice.line_items, start=1)
    ]
    totals_check = _check_totals(invoice, line_item_checks, tolerance)

    result = MathValidationResult(line_item_checks=line_item_checks, totals_check=totals_check)
    _log_result(filename, result)
    return result
