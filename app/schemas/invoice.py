"""
Invoice extraction schemas.

These are the structured types that the LLM must populate.
We use standard dataclasses with lenient parsing from dictionaries
to avoid strict Pydantic validation errors caused by low-end models.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


def _coerce_float(value: Any) -> Optional[float]:
    """
    Best-effort cast to float.

    Weak local models sometimes ignore the "digits and decimal point only"
    instruction and emit numbers as strings anyway -- "1234.56", "$1,234.56",
    even "1234.56 USD". Rather than let a stray string silently corrupt
    downstream math (or blow up validation later), normalize here.
    Returns None if the value genuinely isn't a number, never raises.
    """
    if value is None:
        return None
    if isinstance(value, bool):  # bool is a subclass of int -- exclude it
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip()
        for symbol in ("$", "€", "£", ","):
            cleaned = cleaned.replace(symbol, "")
        cleaned = cleaned.strip()
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


@dataclass
class LineItem:
    """A single line item from the invoice."""
    description: Optional[str] = None
    quantity: Optional[float] = None
    unit_price: Optional[float] = None
    amount: Optional[float] = None


@dataclass
class ExtractedInvoice:
    """
    All structured fields extracted from a vendor invoice or receipt.
    """
    vendor_name: Optional[str] = None
    invoice_number: Optional[str] = None
    invoice_date: Optional[str] = None  # Stored as string YYYY-MM-DD
    due_date: Optional[str] = None      # Stored as string YYYY-MM-DD
    total_amount: Optional[float] = None
    currency: Optional[str] = None
    payment_terms: Optional[str] = None
    tax_type: Optional[str] = None
    tax_amount: Optional[float] = None
    document_type: str = "unknown"
    extraction_confidence: Optional[float] = None
    line_items: list[LineItem] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExtractedInvoice:
        """Leniently parse a dictionary into the ExtractedInvoice dataclass."""
        if not data:
            data = {}

        raw_line_items = data.get("line_items")
        line_items = []
        if isinstance(raw_line_items, list):
            for item in raw_line_items:
                if isinstance(item, dict):
                    line_items.append(LineItem(
                        description=item.get("description"),
                        quantity=_coerce_float(item.get("quantity")),
                        unit_price=_coerce_float(item.get("unit_price")),
                        amount=_coerce_float(item.get("amount")),
                    ))

        return cls(
            vendor_name=data.get("vendor_name"),
            invoice_number=data.get("invoice_number"),
            invoice_date=data.get("invoice_date"),
            due_date=data.get("due_date"),
            total_amount=_coerce_float(data.get("total_amount")),
            currency=data.get("currency"),
            payment_terms=data.get("payment_terms"),
            tax_type=data.get("tax_type"),
            tax_amount=_coerce_float(data.get("tax_amount")),
            document_type=data.get("document_type", "unknown"),
            extraction_confidence=_coerce_float(data.get("extraction_confidence")),
            line_items=line_items
        )