"""
Provider-agnostic Odoo interface.

Both the real XML-RPC client and the in-memory mock implement this same
shape, so app/services/odoo_service.py (and everything upstream of it) never
needs to know which one is actually active -- that's purely a config.odoo.mode
("mock" | "live") switch.
"""
from __future__ import annotations

from typing import Protocol

from app.schemas.invoice import ExtractedInvoice


class OdooClient(Protocol):
    def find_or_create_partner(self, vendor_name: str) -> int:
        """Return the res.partner id for this vendor, creating one if none exists."""
        ...

    def create_vendor_bill(self, partner_id: int, invoice: ExtractedInvoice) -> int:
        """Create a draft account.move (move_type=in_invoice) and return its id."""
        ...

    def post_bill(self, move_id: int) -> None:
        """Validate/post the draft bill (Odoo's action_post)."""
        ...

    def add_chatter_note(self, move_id: int, note: str) -> None:
        """Attach an audit note to the bill's chatter (message_post)."""
        ...
