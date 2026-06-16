"""
In-memory Odoo stand-in -- lets the full pipeline run and be verified with
no live Odoo instance reachable. config.odoo.mode defaults to "mock" for
exactly this reason. Mirrors the same id-allocation and posting semantics as
the real client closely enough that switching to "live" is just a config +
.env change, no code changes anywhere upstream.
"""
from __future__ import annotations

import logging

from app.schemas.invoice import ExtractedInvoice

logger = logging.getLogger(__name__)


class MockOdooClient:
    def __init__(self):
        self._partners: dict[str, int] = {}
        self._next_partner_id = 1
        self._next_move_id = 1
        self.bills: dict[int, dict] = {}  # move_id -> {partner_id, state, notes, invoice}

    def find_or_create_partner(self, vendor_name: str) -> int:
        if vendor_name in self._partners:
            return self._partners[vendor_name]
        partner_id = self._next_partner_id
        self._next_partner_id += 1
        self._partners[vendor_name] = partner_id
        logger.info("[mock-odoo] created partner #%d for '%s'", partner_id, vendor_name)
        return partner_id

    def create_vendor_bill(self, partner_id: int, invoice: ExtractedInvoice) -> int:
        move_id = self._next_move_id
        self._next_move_id += 1
        self.bills[move_id] = {
            "partner_id": partner_id,
            "state": "draft",
            "notes": [],
            "invoice": invoice,
        }
        logger.info("[mock-odoo] created draft bill #%d for partner #%d", move_id, partner_id)
        return move_id

    def post_bill(self, move_id: int) -> None:
        self.bills[move_id]["state"] = "posted"
        logger.info("[mock-odoo] posted bill #%d", move_id)

    def add_chatter_note(self, move_id: int, note: str) -> None:
        self.bills[move_id]["notes"].append(note)
        logger.info("[mock-odoo] chatter note on bill #%d: %s", move_id, note)
