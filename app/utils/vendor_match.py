"""
Vendor name normalization + matching against the Approved Supplier List.

Why not exact string match: the same legal entity is written differently
invoice-to-invoice ("Functional Software, Inc. (Sentry)" vs the list's
"Functional Software, Inc.") -- but two *different* entities can also share a
casual brand name, so this errs toward flagging uncertain matches for a human
rather than ever silently auto-approving on a loose match.

  - Exact match on the normalized name  -> confident auto-pass.
  - Fuzzy match >= fuzzy_threshold but not exact -> still "matched", but NOT
    confident. The validation service must route this to human review rather
    than auto-post -- never silently treated as approved.
  - Below threshold -> not matched at all (NEW_VENDOR_SE territory).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from app.core.config import get_settings

_PAREN_RE = re.compile(r"\s*\([^)]*\)")
_PUNCT_RE = re.compile(r"[.,]")
_WS_RE = re.compile(r"\s+")


def normalize_vendor_name(name: str | None) -> str:
    """Lowercase, strip parenthetical brand suffixes, punctuation, and
    configured legal suffixes (Ltd, Inc, LLC, SARL, Pty, Corp, ...)."""
    if not name:
        return ""
    suffixes = get_settings().vendor_match.strip_suffixes

    text = _PAREN_RE.sub("", name)  # drop parenthetical brand names, e.g. "(Sentry)"
    text = _PUNCT_RE.sub("", text)
    words = text.split()
    suffix_set = {s.lower().rstrip(".") for s in suffixes}
    words = [w for w in words if w.lower().rstrip(".") not in suffix_set]
    text = _WS_RE.sub(" ", " ".join(words)).strip().lower()
    return text


@dataclass
class MatchResult:
    matched:        bool
    confident:      bool          # True only on an exact normalized match
    supplier_name:  str | None    # the approved list's canonical name, if matched
    score:          float         # rapidfuzz token_sort_ratio, 0-100


def match_against_approved(vendor_name: str | None, approved: list) -> MatchResult:
    """`approved` is a list of objects exposing `.vendor_name` and
    `.normalized_name` (see app/repositories/supplier_repo.py's ApprovedSupplier)."""
    if not vendor_name:
        return MatchResult(matched=False, confident=False, supplier_name=None, score=0.0)

    threshold = get_settings().vendor_match.fuzzy_threshold
    normalized = normalize_vendor_name(vendor_name)

    best_score = 0.0
    best_supplier = None
    for supplier in approved:
        if supplier.normalized_name == normalized:
            return MatchResult(
                matched=True, confident=True, supplier_name=supplier.vendor_name, score=100.0
            )
        score = fuzz.token_sort_ratio(normalized, supplier.normalized_name)
        if score > best_score:
            best_score, best_supplier = score, supplier.vendor_name

    if best_score >= threshold:
        return MatchResult(matched=True, confident=False, supplier_name=best_supplier, score=best_score)

    return MatchResult(matched=False, confident=False, supplier_name=None, score=best_score)
