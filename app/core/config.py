"""
Central configuration loader.

Reads `config/settings.yaml` once and caches it for the process lifetime.
Everything tunable (LLM params, paths, etc.) comes from that file.

Usage anywhere in the app:
    from app.core.config import get_settings
    cfg = get_settings()
    model = cfg.llm.model
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

# Always resolve relative to the project root (two levels up from this file:
# app/core/config.py → app/ → project root)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_FILE  = _PROJECT_ROOT / "config" / "settings.yaml"
_SUPPLIER_OVERRIDES_FILE = _PROJECT_ROOT / "config" / "approved_suppliers_overrides.yaml"

# Load .env into os.environ exactly once, at import time, before anything in
# this module reads an env var (OdooConfig's field(default_factory=...)
# below runs os.environ.get(...) when a Settings instance is built, which
# always happens after this module has fully imported). Without this call,
# .env's values never reach os.environ -- they'd just sit in the file
# unused, and ODOO_USERNAME/ODOO_PASSWORD/ODOO_EMAIL would silently read back
# as "" even with a fully filled-in .env (this was a real bug: Odoo
# authentication failed with empty credentials despite .env being correct,
# because nothing ever called load_dotenv()).
load_dotenv(_PROJECT_ROOT / ".env")


# ── Typed sub-configs ─────────────────────────────────────────────────────────

@dataclass
class LLMConfig:
    model:               str
    base_url:            str
    temperature:         float
    top_p:                float
    top_k:                int
    num_ctx:              int
    num_predict:          int
    keep_alive:           str
    reasoning:            bool | str | None
    max_repair_retries:   int


@dataclass
class PathsConfig:
    inbox:     Path
    processed: Path
    logs:      Path
    log_file:  str
    db:        Path
    approved_suppliers_xlsx: Path


@dataclass
class ValidationConfig:
    # Absolute tolerance (same units as total_amount) for the deterministic
    # qty*unit_price≈amount and sum(line items)+tax≈total_amount checks in
    # app/services/math_validation.py.
    amount_tolerance: float


@dataclass
class CompletenessConfig:
    # Field names (from app/schemas/invoice.py) that must be present for an
    # invoice to be eligible for auto-posting. See app/services/validation_service.py.
    mandatory_fields: list[str] = field(default_factory=list)
    # Case-insensitive substrings of payment_terms that exempt due_date from
    # the mandatory list above (card-on-file / auto-charge documents).
    immediate_payment_terms_markers: list[str] = field(default_factory=list)


@dataclass
class VendorMatchConfig:
    fuzzy_threshold: float = 90.0
    strip_suffixes: list[str] = field(default_factory=list)


@dataclass
class OdooConfig:
    mode:      str  # "mock" | "live"
    url:       str
    db:        str
    auto_post: bool
    # Credentials are never stored in YAML -- read from the environment so
    # they can live in a gitignored .env without ever touching version control.
    # Odoo's XML-RPC authenticate() call names this parameter "username", but
    # in practice the value it expects is the user's login email -- ODOO_EMAIL
    # is the semantically correct field to fill in. ODOO_USERNAME is kept for
    # backward compatibility (e.g. a non-email login on self-hosted Odoo); see
    # the `login` property below for the resolution order.
    email:    str = field(default_factory=lambda: os.environ.get("ODOO_EMAIL", ""))
    username: str = field(default_factory=lambda: os.environ.get("ODOO_USERNAME", ""))
    password: str = field(default_factory=lambda: os.environ.get("ODOO_PASSWORD", ""))

    @property
    def login(self) -> str:
        """The value actually sent as the `login`/`username` argument to
        Odoo's authenticate() call. Prefers ODOO_EMAIL since that's what
        Odoo's login actually is in the overwhelming majority of setups;
        falls back to ODOO_USERNAME so existing .env files keep working
        unchanged."""
        return self.email or self.username


@dataclass
class Settings:
    llm:          LLMConfig
    paths:        PathsConfig
    validation:   ValidationConfig
    completeness: CompletenessConfig
    vendor_match: VendorMatchConfig
    odoo:         OdooConfig

    @property
    def project_root(self) -> Path:
        return _PROJECT_ROOT


# ── Loader ────────────────────────────────────────────────────────────────────

def _load_yaml() -> dict:
    if not _CONFIG_FILE.exists():
        raise FileNotFoundError(
            f"Config file not found: {_CONFIG_FILE}\n"
            "Make sure config/settings.yaml exists in the project root."
        )
    with open(_CONFIG_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _parse_settings(raw: dict) -> Settings:
    llm_raw   = raw.get("llm", {})
    paths_raw = raw.get("paths", {})

    llm = LLMConfig(
        model              = llm_raw.get("model",       "gemma3:4b"),
        base_url           = llm_raw.get("base_url",    "http://localhost:11434"),
        temperature        = float(llm_raw.get("temperature", 0.0)),
        top_p              = float(llm_raw.get("top_p",       0.9)),
        top_k              = int(llm_raw.get("top_k",         40)),
        num_ctx            = int(llm_raw.get("num_ctx",        2048)),
        num_predict        = int(llm_raw.get("num_predict",    1024)),
        keep_alive         = str(llm_raw.get("keep_alive",    "10m")),
        # False, not None: explicitly disable hidden "thinking" mode so
        # reasoning-capable models (Qwen3 family) don't burn num_predict on
        # hidden <think> tokens and return empty content. Harmless no-op for
        # models that don't support reasoning (e.g. Gemma).
        reasoning          = llm_raw.get("reasoning", False),
        # How many times the model gets to repair its own malformed JSON
        # (fed its bad output + the parse error) before we give up and flag
        # the file for manual review.
        max_repair_retries = int(llm_raw.get("max_repair_retries", 1)),
    )

    paths = PathsConfig(
        inbox     = resolve_path(paths_raw.get("inbox",    "data/inbox")),
        processed = resolve_path(paths_raw.get("processed","data/processed")),
        logs      = resolve_path(paths_raw.get("logs",     "logs")),
        log_file  = paths_raw.get("log_file", "agent.log"),
        db        = resolve_path(paths_raw.get("db",       "data/agent.db")),
        approved_suppliers_xlsx = resolve_path(
            paths_raw.get("approved_suppliers_xlsx", "data/Approved Supplier List.xlsx")
        ),
    )

    validation_raw = raw.get("validation", {})
    validation = ValidationConfig(
        amount_tolerance = float(validation_raw.get("amount_tolerance", 0.01)),
    )

    completeness_raw = raw.get("completeness", {})
    completeness = CompletenessConfig(
        mandatory_fields = list(completeness_raw.get("mandatory_fields") or []),
        immediate_payment_terms_markers = list(
            completeness_raw.get("immediate_payment_terms_markers") or []
        ),
    )

    vendor_match_raw = raw.get("vendor_match", {})
    vendor_match = VendorMatchConfig(
        fuzzy_threshold = float(vendor_match_raw.get("fuzzy_threshold", 90.0)),
        strip_suffixes  = list(vendor_match_raw.get("strip_suffixes") or []),
    )

    odoo_raw = raw.get("odoo", {})
    odoo = OdooConfig(
        mode      = odoo_raw.get("mode", "mock"),
        url       = odoo_raw.get("url", "http://localhost:8069"),
        db        = odoo_raw.get("db", "huma"),
        auto_post = bool(odoo_raw.get("auto_post", True)),
    )

    return Settings(
        llm=llm, paths=paths, validation=validation,
        completeness=completeness, vendor_match=vendor_match, odoo=odoo,
    )


def get_settings() -> Settings:
    """Load settings from config/settings.yaml on every call."""
    return _parse_settings(_load_yaml())


def resolve_path(relative: str | Path) -> Path:
    """Resolve a path from config relative to the project root (absolute
    paths pass through unchanged)."""
    p = Path(relative)
    return p if p.is_absolute() else _PROJECT_ROOT / p


def get_supplier_overrides() -> list[dict]:
    """Vendors approved via manual Supplier Evaluation (SE) review, additive
    on top of the xlsx. Not cached -- meant to be hand-edited (or edited via
    append_supplier_override) and re-read on every check so a newly-approved
    vendor is picked up without a restart."""
    if not _SUPPLIER_OVERRIDES_FILE.exists():
        return []
    with open(_SUPPLIER_OVERRIDES_FILE, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("overrides") or []


def append_supplier_override(vendor_name: str, approved_by: str = "manual") -> None:
    """Record a vendor as SE-approved. Rewrites the file (not a plain
    append) so the YAML stays valid across repeated programmatic edits."""
    from datetime import date

    overrides = get_supplier_overrides()
    overrides.append({
        "vendor_name": vendor_name,
        "approved_by": approved_by,
        "date_approved": date.today().isoformat(),
    })
    header = (
        "# Vendors approved via manual Supplier Evaluation (SE) review AFTER the original\n"
        "# Approved Supplier List.xlsx was issued. Additive only -- this agent never edits\n"
        "# the xlsx itself. Entries here are picked up on the next check, no restart needed.\n\n"
    )
    with open(_SUPPLIER_OVERRIDES_FILE, "w", encoding="utf-8") as f:
        f.write(header)
        yaml.safe_dump({"overrides": overrides}, f, sort_keys=False, allow_unicode=True)
