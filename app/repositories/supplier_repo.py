"""
Loads the Approved Supplier List the agent validates vendors against.

Source of truth = the xlsx Finance provides (never mutated by the agent).
Additive overlay = config/approved_suppliers_overrides.yaml, appended to when
a vendor clears manual Supplier Evaluation (SE) review after the xlsx was
issued (see app/core/config.py's append_supplier_override).
"""
from __future__ import annotations

from dataclasses import dataclass

import openpyxl

from app.core.config import get_settings, get_supplier_overrides
from app.utils.vendor_match import normalize_vendor_name


@dataclass
class ApprovedSupplier:
    vendor_name:            str
    normalized_name:        str
    default_payment_terms:  str | None = None
    currency:                str | None = None
    source:                 str = "xlsx"  # "xlsx" | "override"


def load_approved_suppliers() -> list[ApprovedSupplier]:
    """Re-reads the xlsx + overrides file on every call (cheap, and lets a
    freshly-approved vendor or edited xlsx be picked up without a restart)."""
    path = get_settings().paths.approved_suppliers_xlsx
    suppliers: list[ApprovedSupplier] = []

    if path.exists():
        wb = openpyxl.load_workbook(path, data_only=True)
        ws = wb.worksheets[0]
        rows = list(ws.iter_rows(values_only=True))

        header_idx = next(
            (i for i, r in enumerate(rows) if r and r[0] == "Supplier ID"), None
        )
        if header_idx is not None:
            for row in rows[header_idx + 1:]:
                if not row or row[0] is None:
                    break  # blank row = end of the table (a trailing "Note:" row follows)
                vendor_name, status = row[1], row[4]
                if not vendor_name or status != "Approved":
                    continue
                suppliers.append(
                    ApprovedSupplier(
                        vendor_name=vendor_name,
                        normalized_name=normalize_vendor_name(vendor_name),
                        default_payment_terms=row[5],
                        currency=row[6],
                        source="xlsx",
                    )
                )

    for override in get_supplier_overrides():
        name = override.get("vendor_name")
        if not name:
            continue
        suppliers.append(
            ApprovedSupplier(
                vendor_name=name,
                normalized_name=normalize_vendor_name(name),
                source="override",
            )
        )

    return suppliers
