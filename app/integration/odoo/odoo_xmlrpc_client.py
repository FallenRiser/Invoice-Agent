"""
Real Odoo integration over XML-RPC.

Auth + call pattern follows Odoo's standard external API:
  - /xmlrpc/2/common -> authenticate(db, username, password, {}) -> uid
  - /xmlrpc/2/object -> execute_kw(db, uid, password, model, method, args, kwargs)

Field mapping:
  - move_type = "in_invoice" (Vendor Bill)
  - partner_id <- find-or-create res.partner by exact name match
  - ref <- invoice.invoice_number (also lets Odoo's own native per-partner
    duplicate-reference warning serve as a second, independent line of defense
    on top of our own duplicate check in validation_service.py)
  - invoice_date / invoice_date_due <- invoice.invoice_date / due_date (already
    plain "YYYY-MM-DD" strings in this app's schema -- no .isoformat() needed)
  - invoice_line_ids <- one (0, 0, {...}) tuple per LineItem
  - currency_id <- resolved from invoice.currency (ISO code, e.g. "GBP") against
    res.currency.name. Odoo only activates a handful of currencies by default --
    everything else exists in the DB but is inactive and invisible to a plain
    search(), so we search with active_test=False and flip the record active
    if needed. Without this, every bill silently falls back to the company's
    base currency regardless of what's on the invoice.
"""
from __future__ import annotations

import logging
import xmlrpc.client

from app.schemas.invoice import ExtractedInvoice

logger = logging.getLogger(__name__)


class OdooXmlRpcClient:
    def __init__(self, url: str, db: str, username: str, password: str):
        self.db = db
        self.username = username
        self.password = password
        self.common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
        self.models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")
        self.uid = self.common.authenticate(db, username, password, {})
        if not self.uid:
            raise RuntimeError(
                "Odoo authentication failed -- check ODOO_EMAIL (or ODOO_USERNAME) and "
                "ODOO_PASSWORD in .env, and confirm those credentials work in the Odoo "
                "web UI login at this same url/db."
            )

    def _execute(self, model: str, method: str, *args, **kwargs):
        return self.models.execute_kw(self.db, self.uid, self.password, model, method, list(args), kwargs)

    def find_or_create_partner(self, vendor_name: str) -> int:
        existing = self._execute("res.partner", "search", [("name", "=", vendor_name)], limit=1)
        if existing:
            return existing[0]
        logger.info("Creating new res.partner for vendor '%s'", vendor_name)
        return self._execute("res.partner", "create", {"name": vendor_name, "company_type": "company"})

    def _resolve_currency_id(self, currency_code: str | None) -> int | None:
        """Look up res.currency by ISO code (e.g. "GBP"), activating it if Odoo
        shipped it inactive. Returns None (and logs a warning) if the code
        doesn't match any known currency -- callers should leave currency_id
        unset in that case rather than guess, so the bill falls back to the
        company default instead of erroring out."""
        if not currency_code:
            return None
        ids = self._execute(
            "res.currency", "search", [("name", "=", currency_code.upper())],
            context={"active_test": False},
        )
        if not ids:
            logger.warning("Currency '%s' not found in Odoo -- bill will use the company default currency", currency_code)
            return None
        currency_id = ids[0]
        self._execute("res.currency", "write", [currency_id], {"active": True})
        return currency_id

    def _resolve_purchase_tax_id(self, rate_percent: float, tax_name: str | None = None) -> int | None:
        """Find (or create) a percentage-based purchase tax matching this rate,
        so the bill carries a real account.tax record instead of a flat
        compensating line. Tolerant match (±0.01) to absorb float rounding;
        activates it if Odoo shipped it inactive, same idea as
        _resolve_currency_id above."""
        rate_percent = round(rate_percent, 2)
        if rate_percent <= 0:
            return None
        ids = self._execute(
            "account.tax", "search",
            [
                ("type_tax_use", "=", "purchase"),
                ("amount_type", "=", "percent"),
                ("amount", ">=", rate_percent - 0.01),
                ("amount", "<=", rate_percent + 0.01),
            ],
            context={"active_test": False}, limit=1,
        )
        if ids:
            self._execute("account.tax", "write", [ids[0]], {"active": True})
            return ids[0]
        
        fallback_name = tax_name if tax_name else f"VAT {rate_percent:g}%"
        logger.info("Creating new %.2g%% purchase tax (no existing match)", rate_percent)
        return self._execute("account.tax", "create", {
            "name": fallback_name,
            "amount": rate_percent,
            "amount_type": "percent",
            "type_tax_use": "purchase",
        })

    def create_vendor_bill(self, partner_id: int, invoice: ExtractedInvoice) -> int:
        lines = [
            (0, 0, {
                "name": item.description,
                "quantity": float(item.quantity),
                "price_unit": float(item.unit_price),
            })
            for item in invoice.line_items
        ] or [(0, 0, {"name": invoice.vendor_name or "Invoice", "quantity": 1, "price_unit": float(invoice.total_amount or 0)})]

        # The LLM extracts tax_amount and tax_type, so we can use those directly
        # to apply a *real* percentage tax via Odoo's tax engine (find-or-create an account.tax).
        # We calculate the rate using the sum of line items.
        # Falls back to computing the gap if tax_amount isn't extracted.
        tax_delta = 0.0
        lines_sum = sum(
            (item.amount if item.amount is not None else (item.quantity or 1) * (item.unit_price or 0))
            for item in invoice.line_items
        ) if invoice.line_items else 0.0

        if invoice.tax_amount is not None:
            tax_delta = float(invoice.tax_amount)
        elif invoice.line_items and invoice.total_amount is not None:
            tax_delta = float(invoice.total_amount) - float(lines_sum)

        if abs(tax_delta) >= 0.01:
            rate_percent = (tax_delta / lines_sum * 100) if lines_sum > 0 else 0
            tax_id = self._resolve_purchase_tax_id(rate_percent, tax_name=invoice.tax_type) if 0 < rate_percent <= 100 else None
            if tax_id:
                lines = [(t, f, {**vals, "tax_ids": [(6, 0, [tax_id])]}) for (t, f, vals) in lines]
            else:
                tax_label = invoice.tax_type if invoice.tax_type else "Tax / VAT (per invoice total)"
                lines.append((0, 0, {"name": tax_label, "quantity": 1, "price_unit": tax_delta}))

        values = {
            "move_type": "in_invoice",
            "partner_id": partner_id,
            "ref": invoice.invoice_number,
            "invoice_date": invoice.invoice_date or False,
            "invoice_line_ids": lines,
        }
        if invoice.due_date:
            values["invoice_date_due"] = invoice.due_date

        currency_id = self._resolve_currency_id(invoice.currency)
        if currency_id:
            values["currency_id"] = currency_id

        return self._execute("account.move", "create", values)

    def post_bill(self, move_id: int) -> None:
        self._execute("account.move", "action_post", [move_id])

    def add_chatter_note(self, move_id: int, note: str) -> None:
        self._execute("account.move", "message_post", [move_id], body=note)
