"""
Ollama LLM extractor — the heart of the extraction layer.

Uses `langchain-ollama` ChatOllama with `format="json"`
so the model is constrained to return JSON. We parse it leniently into
our data classes to avoid strict validation errors from lower-end models.

Flow:
  1. Build system prompt (invoice extraction instructions).
  2. Instantiate ChatOllama with all params (logged on every run).
  3. Invoke the model directly.
  4. Clean + parse the raw JSON string leniently into a dictionary.
  5. If parsing fails, give the model ONE chance to repair its own output.
  6. On repeated failure → return None (caller decides what to do).

Why this needs to be lenient: small/low-end local models (gemma3:4b,
qwen3.5:0.8b, ...) are unreliable at pure structured output. Two failure
modes show up in practice:
  - Hybrid "thinking" models (the Qwen3 family) emit hidden
    <think>...</think> reasoning before the JSON. With a small num_predict
    budget the model can burn its entire token allowance on that hidden
    reasoning and never emit any JSON at all — the response comes back
    completely empty. `reasoning=False` (passed to ChatOllama, which maps to
    Ollama's `think` option) tells the model to skip that step entirely.
    It's a no-op for models that don't support reasoning (e.g. Gemma).
  - Even non-reasoning small models sometimes wrap the JSON in markdown
    fences or add a sentence of preamble/postamble despite format="json".
    `_parse_json_leniently` strips that noise and, failing that, extracts
    the widest {...} span before giving up.

Params logged on every invocation:
  model, base_url, temperature, top_p, top_k, num_ctx, num_predict (max_tokens),
  reasoning, max_repair_retries
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_ollama import ChatOllama

from app.core.config import get_settings
from app.schemas.invoice import ExtractedInvoice

logger = logging.getLogger(__name__)

# ── JSON cleanup helpers ──────────────────────────────────────────────────────

_THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE | re.IGNORECASE)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _clean_json_text(content: str) -> str:
    """Strip the noise small/reasoning models tend to wrap JSON in."""
    text = content.strip()
    text = _THINK_TAG_RE.sub("", text).strip()
    text = _CODE_FENCE_RE.sub("", text).strip()
    return text


def _parse_json_leniently(content: str) -> tuple[dict[str, Any] | None, str | None]:
    """
    Try increasingly forgiving strategies to turn *content* into a dict.

    Returns (parsed_dict, None) on success, or (None, error_message) on
    failure so the caller can log/repair-retry with a concrete reason.
    """
    if not content or not content.strip():
        return None, "empty response from model (likely ran out of num_predict tokens)"

    cleaned = _clean_json_text(content)
    if not cleaned:
        return None, "response contained only <think>/markdown noise, no JSON"

    try:
        return json.loads(cleaned), None
    except json.JSONDecodeError as exc:
        first_error = str(exc)

    # Fallback: the model added prose around the JSON object
    # ("Sure! Here's the JSON: {...} Let me know if you need anything else.")
    # — grab the widest {...} span and try again.
    match = _JSON_OBJECT_RE.search(cleaned)
    if match:
        try:
            return json.loads(match.group(0)), None
        except json.JSONDecodeError:
            pass

    return None, first_error

# ── System prompt ─────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are an expert accounts-payable data extraction system. Your sole job is to
extract structured fields from vendor invoice or receipt text and return them as
a single JSON object that strictly matches the provided schema.

Rules:
- Extract data ONLY from the text provided. Never invent or guess values.
- Use null for any field genuinely not present in the document.
- Dates: always convert to YYYY-MM-DD format regardless of how they appear.
- vendor_name: the entity ISSUING the invoice (the seller), never the buyer/recipient.
  PDF text extraction sometimes breaks a company's legal name across two
  lines, or interleaves an unrelated stamp/title word (e.g. "INVOICE",
  "PAID", "RECEIPT", "DRAFT", "COPY") right next to or inside the name
  because of how the original document was laid out. Reconstruct the full
  legal name and EXCLUDE those stamp/title words from it. Example: if the
  text contains "Acme Corp INVOICE" on one line followed by "GmbH" on the
  next line, and then "PAID" on its own line, the correct vendor_name is
  "Acme Corp GmbH" -- not "GmbH PAID" and not "Acme Corp INVOICE GmbH".
- total_amount, tax_amount, unit_price, amount: digits and decimal point only.
  No currency symbols ($ € £), no thousands separators (commas/dots used as separators).
- currency: ISO 4217 3-letter code (USD, EUR, GBP, INR, etc.).
- document_type: "receipt" if the document confirms a payment already collected
  (e.g. "This is a receipt for a charge to your card"). Otherwise "invoice".
- line_items: extract ALL itemised lines. Empty list if none are present.
- extraction_confidence: your honest self-assessed confidence (0.0-1.0) that
  ALL extracted fields are correct. Be conservative.

Expected JSON format:
{
  "vendor_name": "string or null",
  "invoice_number": "string or null",
  "invoice_date": "string (YYYY-MM-DD) or null",
  "due_date": "string (YYYY-MM-DD) or null",
  "total_amount": "number or null",
  "currency": "string or null",
  "payment_terms": "string or null",
  "tax_type": "string or null",
  "tax_amount": "number or null",
  "document_type": "invoice, receipt, or unknown",
  "extraction_confidence": "number (0.0-1.0)",
  "line_items": [
    {
      "description": "string or null",
      "quantity": "number or null",
      "unit_price": "number or null",
      "amount": "number or null"
    }
  ]
}
"""


# ── Extractor class ───────────────────────────────────────────────────────────

class OllamaExtractor:
    """
    Wraps ChatOllama + structured output into a single `.extract()` call.

    Parameters mirror Ollama's API options so callers can tune the model
    without touching this file.
    """

    def __init__(
        self,
        model:               str        | None = None,
        base_url:            str        | None = None,
        temperature:         float      | None = None,
        top_p:               float      | None = None,
        top_k:               int        | None = None,
        num_ctx:             int        | None = None,
        num_predict:         int        | None = None,
        keep_alive:          str        | None = None,
        reasoning:           bool | str | None = None,
        max_repair_retries:  int        | None = None,
    ) -> None:
        # Fall back to config/settings.yaml for any param not explicitly passed
        cfg = get_settings().llm
        self.model              = model              if model              is not None else cfg.model
        self.base_url           = base_url           if base_url           is not None else cfg.base_url
        self.temperature        = temperature        if temperature        is not None else cfg.temperature
        self.top_p              = top_p              if top_p              is not None else cfg.top_p
        self.top_k              = top_k              if top_k              is not None else cfg.top_k
        self.num_ctx            = num_ctx            if num_ctx            is not None else cfg.num_ctx
        self.num_predict        = num_predict        if num_predict        is not None else cfg.num_predict
        self.keep_alive         = keep_alive         if keep_alive         is not None else cfg.keep_alive
        self.reasoning          = reasoning          if reasoning          is not None else cfg.reasoning
        self.max_repair_retries = max_repair_retries if max_repair_retries is not None else cfg.max_repair_retries

        # Build the base LLM using the resolved (config-or-override) values.
        # reasoning=False forces hybrid "thinking" models (Qwen3 family) to
        # skip hidden <think> reasoning and answer directly — see module
        # docstring for why this matters for small models + tight token budgets.
        self._llm = ChatOllama(
            model=self.model,
            base_url=self.base_url,
            temperature=self.temperature,
            top_p=self.top_p,
            top_k=self.top_k,
            num_ctx=self.num_ctx,
            num_predict=self.num_predict,
            keep_alive=self.keep_alive,
            format="json",
            reasoning=self.reasoning,
        )

    # ── Internal helpers ──────────────────────────────────────────────

    def _log_params(self) -> None:
        """Log the model identity and all tunable parameters."""
        logger.info(
            "[LLM]  Model      : %s  (base_url=%s)",
            self.model, self.base_url,
        )
        logger.info(
            "[LLM]  Parameters : temperature=%.2f  top_p=%.2f  top_k=%d  "
            "num_ctx=%d  num_predict=%d  keep_alive=%s  reasoning=%s  max_repair_retries=%d",
            self.temperature, self.top_p, self.top_k,
            self.num_ctx, self.num_predict, self.keep_alive,
            self.reasoning, self.max_repair_retries,
        )

    @staticmethod
    def _log_raw(raw: Any) -> None:
        """Log the raw AIMessage content from the LLM."""
        try:
            content = raw.content if hasattr(raw, "content") else str(raw)
            display = content if content and content.strip() else "<empty>"
            # Pretty-print if it looks like JSON, otherwise log as-is
            try:
                parsed_json = json.loads(display)
                pretty = json.dumps(parsed_json, indent=2, default=str)
            except (json.JSONDecodeError, TypeError):
                pretty = display
            logger.debug("[LLM]  Raw output:\n%s", pretty)
        except Exception as exc:
            logger.debug("[LLM]  Could not display raw output: %s", exc)

    @staticmethod
    def _log_parsed(invoice: ExtractedInvoice) -> None:
        """Log the parsed, validated ExtractedInvoice fields."""
        logger.info("[LLM]  Parsed output:")
        logger.info("         vendor_name        : %s", invoice.vendor_name)
        logger.info("         invoice_number     : %s", invoice.invoice_number)
        logger.info("         invoice_date       : %s", invoice.invoice_date)
        logger.info("         due_date           : %s", invoice.due_date)
        logger.info("         total_amount       : %s %s", invoice.total_amount, invoice.currency)
        logger.info("         payment_terms      : %s", invoice.payment_terms)
        logger.info("         tax_type           : %s  tax_amount: %s", invoice.tax_type, invoice.tax_amount)
        logger.info("         document_type      : %s", invoice.document_type)
        logger.info("         confidence         : %s", invoice.extraction_confidence)
        logger.info("         line_items count   : %d", len(invoice.line_items))
        for i, item in enumerate(invoice.line_items, start=1):
            logger.info(
                "           [%d] %s  qty=%s  unit_price=%s  amount=%s",
                i, item.description, item.quantity, item.unit_price, item.amount,
            )

    # ── Public API ────────────────────────────────────────────────────

    def extract(self, text: str, filename: str = "") -> ExtractedInvoice | None:
        """
        Run the extraction pipeline on *text* (PDF text or image description).

        Returns a validated `ExtractedInvoice` on success, or `None` if the
        LLM response could not be parsed (even after a repair retry) — the
        caller should route None to a human review queue. We never guess at
        partial data: a parse failure means the file gets flagged, not posted.
        """
        label = f"'{filename}'" if filename else "input"
        logger.info("[LLM]  Starting extraction for %s", label)
        self._log_params()

        messages: list[BaseMessage] = [
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=text),
        ]

        max_attempts = self.max_repair_retries + 1
        last_error: str | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                raw = self._llm.invoke(messages)
            except Exception as exc:
                # Connection/server-level failures (timeouts, dropped
                # sockets, Ollama crashing) aren't something a repair-retry
                # can fix — fail closed immediately.
                logger.error(
                    "[LLM]  LLM invocation failed for %s (attempt %d/%d): %s",
                    label, attempt, max_attempts, exc, exc_info=True,
                )
                return None

            self._log_raw(raw)
            content = raw.content if hasattr(raw, "content") else str(raw)

            parsed_json, error = _parse_json_leniently(content)

            if parsed_json is not None:
                try:
                    invoice = ExtractedInvoice.from_dict(parsed_json)
                except Exception as exc:
                    logger.error(
                        "[LLM]  Invoice object creation failed for %s: %s",
                        label, exc,
                    )
                    return None

                self._log_parsed(invoice)
                logger.info(
                    "[LLM]  Extraction complete for %s (confidence=%.2f, attempt %d/%d)",
                    label,
                    invoice.extraction_confidence if invoice.extraction_confidence is not None else -1.0,
                    attempt, max_attempts,
                )
                return invoice

            last_error = error
            logger.warning(
                "[LLM]  JSON parsing failed for %s on attempt %d/%d: %s",
                label, attempt, max_attempts, error,
            )

            if attempt < max_attempts:
                # Give the model exactly one chance to fix its own output —
                # feed back what it said plus the concrete parse error.
                messages.append(AIMessage(content=content))
                messages.append(HumanMessage(content=(
                    "That response was not valid JSON. Parse error: "
                    f"{error}. Reply again with ONLY the corrected JSON "
                    "object — no <think> tags, no markdown fences, no commentary."
                )))

        logger.error(
            "[LLM]  Field extraction permanently failed for %s after %d attempt(s): %s",
            label, max_attempts, last_error,
        )
        return None
