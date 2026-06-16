"""
Thin orchestration layer over the active OdooClient (mock or live -- picked
by config.odoo.mode). This is the single place the rest of the app calls
into to actually push an invoice to Odoo.
"""
from __future__ import annotations

import logging
from functools import lru_cache

from app.core.config import get_settings
from app.schemas.invoice import ExtractedInvoice

logger = logging.getLogger(__name__)


@lru_cache
def get_client():
    settings = get_settings()
    if settings.odoo.mode == "live":
        from app.integration.odoo.odoo_xmlrpc_client import OdooXmlRpcClient

        return OdooXmlRpcClient(
            url=settings.odoo.url,
            db=settings.odoo.db,
            username=settings.odoo.login,
            password=settings.odoo.password,
        )
    if settings.odoo.mode == "mock":
        from app.integration.odoo.mock_odoo_client import MockOdooClient

        return MockOdooClient()
    raise ValueError(f"Unknown odoo.mode: {settings.odoo.mode!r} (expected 'mock' or 'live')")


def post_invoice(invoice: ExtractedInvoice, source_file: str) -> int:
    """Create the vendor bill, post it if odoo.auto_post is true, and leave
    an audit note on the chatter either way. Returns the account.move id."""
    settings = get_settings()
    client = get_client()

    partner_id = client.find_or_create_partner(invoice.vendor_name)
    move_id = client.create_vendor_bill(partner_id, invoice)

    confidence = invoice.extraction_confidence
    confidence_str = f"{confidence:.2f}" if confidence is not None else "n/a"
    note = (
        f"Created by Invoice Processing Agent from '{source_file}' "
        f"(extraction_confidence={confidence_str})."
    )
    client.add_chatter_note(move_id, note)

    if settings.odoo.auto_post:
        client.post_bill(move_id)
        logger.info("[ODOO]  Posted bill #%d for %s (%s)", move_id, invoice.vendor_name, source_file)
    else:
        logger.info("[ODOO]  Left bill #%d as draft (auto_post=false) for %s", move_id, invoice.vendor_name)

    return move_id
