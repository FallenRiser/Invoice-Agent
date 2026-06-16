"""
Every outcome the agent can reach for one invoice, plus small JSON
(de)serialization helpers so an ExtractedInvoice (a plain dataclass, not
Pydantic) can be stored as text in review_queue.extracted_json and read back
later for a human reviewer.
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from app.schemas.invoice import ExtractedInvoice


class Decision(str, Enum):
    POSTED              = "POSTED"
    INCOMPLETE          = "INCOMPLETE"
    NEW_VENDOR_SE        = "NEW_VENDOR_SE"
    DUPLICATE           = "DUPLICATE"
    MATH_MISMATCH        = "MATH_MISMATCH"
    EXTRACTION_FAILED   = "EXTRACTION_FAILED"
    ODOO_ERROR           = "ODOO_ERROR"


@dataclass
class ValidationOutcome:
    decision:               Decision
    reason:                 str
    missing_fields:          list[str]
    normalized_vendor:       Optional[str]
    matched_supplier_name:   Optional[str]


def extracted_to_json(invoice: ExtractedInvoice) -> str:
    """Serialize an ExtractedInvoice for storage in review_queue.extracted_json."""
    return json.dumps(dataclasses.asdict(invoice), default=str)


def extracted_from_json(text: str | None) -> Optional[ExtractedInvoice]:
    """Inverse of extracted_to_json. Returns None on empty/invalid input
    rather than raising -- this is read by a human-facing tool, not a
    decision path, so a malformed row shouldn't crash anything."""
    if not text:
        return None
    try:
        return ExtractedInvoice.from_dict(json.loads(text))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
