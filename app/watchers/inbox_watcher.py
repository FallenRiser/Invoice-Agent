"""
Inbox folder watcher.

Uses the `watchdog` library to monitor `data/inbox/` for new files.
When a file appears:
  1. Log that it was received.
  2. Check its extension -- only .pdf files are processed; anything else
     is logged and left alone.
  3. For PDFs: extract text + tables via pdfplumber.
     - If the PDF appears image-based (very little text), send a note to
       the LLM indicating it received an image-based PDF (vision/OCR TBD).
     - Otherwise send the extracted structured text to the LLM.
  4. LLM (ChatOllama) extracts all invoice fields into an ExtractedInvoice.
  5. Deterministic math validation (independent of the LLM) checks the
     extracted numbers actually add up.
  6. Validation service runs completeness -> approved-supplier (name
     matching) -> duplicate-detection checks, in that fixed order.
  7. Decision:
       - Every check passes AND the math is not a mismatch -> push to Odoo
         (find/create partner, create vendor bill, chatter note, post),
         record it as processed, and move the PDF to `data/processed/`.
       - Anything else (missing fields, unapproved/unconfirmed vendor,
         duplicate invoice number, math mismatch, or extraction failure)
         -> flagged into the human review queue. The PDF stays in
         `data/inbox/` -- only successfully-posted invoices ever move out,
         so a human can find every unresolved file in one place.
  Every outcome, either way, gets one row in the audit log.

The watcher handles both ON_CREATED events (file dropped directly) and
ON_MOVED events (file renamed/moved into the folder, e.g. from a temp
write -- common on Windows and in some copy tools).
"""
from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from app.core.config import get_settings
from app.integration.llm.ollama_extractor import OllamaExtractor
from app.observability import audit_log
from app.repositories import invoice_repo, review_repo
from app.repositories.db import init_db
from app.schemas.review import Decision, extracted_to_json
from app.services import odoo_service
from app.services import validation_service
from app.services.math_validation import MathValidationResult, validate_invoice_math
from app.utils.pdf_extractor import extract_from_pdf

logger = logging.getLogger(__name__)

# Image-based PDF placeholder sent to the LLM so it can attempt
# extraction even when pdfplumber finds no text. The LLM cannot
# actually see the image yet (vision/OCR is a future step), but
# logging and routing are wired up from this point forward.
_IMAGE_PDF_PLACEHOLDER = (
    "[IMAGE-BASED PDF] pdfplumber could not extract readable text from this document. "
    "It is likely a scanned image. Vision/OCR extraction is not yet implemented. "
    "Please flag this invoice for manual review."
)

# How long to wait after a file event before touching the file.
# This gives copy operations time to finish writing before we open it.
_SETTLE_SECONDS = 0.5

# Only these extensions are treated as candidate invoices.
_SUPPORTED_EXTENSIONS = {".pdf"}


def _wait_for_file_stable(path: Path, timeout: float = 10.0) -> bool:
    """
    Poll the file size until it stops changing for _SETTLE_SECONDS.
    Returns True if the file is stable and readable, False on timeout.
    """
    deadline = time.monotonic() + timeout
    last_size = -1
    while time.monotonic() < deadline:
        try:
            size = path.stat().st_size
        except OSError:
            time.sleep(0.2)
            continue
        if size == last_size and size > 0:
            return True
        last_size = size
        time.sleep(_SETTLE_SECONDS)
    return False


def _math_mismatch_summary(math_result: MathValidationResult) -> str:
    """One-line human-readable summary of every mismatch found, for the
    review queue / audit log reason field."""
    parts = []
    for c in math_result.line_item_checks:
        if c.status == "mismatch":
            parts.append(
                f"line {c.index} ({c.description}): {c.quantity}×{c.unit_price}="
                f"{c.computed_amount} but stated {c.stated_amount} (delta {c.delta})"
            )
    t = math_result.totals_check
    if t is not None and t.status == "mismatch":
        parts.append(
            f"totals: line items({t.line_item_sum}) + tax({t.tax_amount}) = "
            f"{t.expected_total} but stated total {t.stated_total} (delta {t.delta})"
        )
    return "; ".join(parts) if parts else "math mismatch detected"


class InboxEventHandler(FileSystemEventHandler):
    """Handles file-system events inside the watched inbox folder."""

    def __init__(self, extractor: OllamaExtractor) -> None:
        super().__init__()
        self._extractor = extractor

    # ── Outcome helpers ────────────────────────────────────────────────

    def _flag_for_review(self, filename: str, decision: Decision, reason: str, invoice=None) -> None:
        """Common path for every non-POSTED outcome: write to the human
        review queue + audit log. The PDF is deliberately NOT moved -- it
        stays in data/inbox/ until a human resolves it."""
        extracted_json = extracted_to_json(invoice) if invoice is not None else None
        flag_id = review_repo.add_flag(filename, decision.value, reason, extracted_json)
        audit_log.write(filename, decision.value, reason, detail=f"review_queue_id={flag_id}")
        logger.warning("[REVIEW]  '%s' flagged for human validation [%s]: %s", filename, decision.value, reason)

    def _move_to_processed(self, path: Path) -> None:
        processed_dir = get_settings().paths.processed
        processed_dir.mkdir(parents=True, exist_ok=True)
        dest = processed_dir / path.name
        shutil.move(str(path), str(dest))
        logger.info("[MOVE]  '%s' moved to processed/: %s", path.name, dest)

    def _process(self, path: Path) -> None:
        """Central processing function called for every new/moved-in file."""

        logger.info("=" * 60)
        logger.info("[INBOX]  File received in inbox: %s", path.name)

        # ── Step 1: file-type check ───────────────────────────────────
        ext = path.suffix.lower()
        if ext not in _SUPPORTED_EXTENSIONS:
            logger.warning(
                "Unsupported file type '%s' -- only %s files are processed. "
                "Leaving '%s' as-is.",
                ext or "(no extension)",
                ", ".join(_SUPPORTED_EXTENSIONS),
                path.name,
            )
            return

        logger.info("[OK]  File type check passed (%s). Processing: %s", ext, path.name)

        # ── Step 2: wait for the file to finish writing ───────────────
        logger.debug("Waiting for file to finish writing...")
        if not _wait_for_file_stable(path):
            logger.error("File '%s' did not stabilise in time -- skipping.", path.name)
            return
        logger.debug("File is stable and ready.")

        # ── Step 3: PDF extraction ────────────────────────────────────
        logger.info("[PDF]  Extracting text from PDF: %s", path.name)
        result = extract_from_pdf(path)

        if result.errors:
            logger.error("Extraction encountered errors for '%s': %s", path.name, result.errors)

        if result.is_image_based:
            logger.warning(
                "[IMAGE]  '%s' appears to be an image-based PDF (scanned). "
                "Sending placeholder to LLM -- vision/OCR extraction is not yet implemented.",
                path.name,
            )
            # Still route through the LLM so the pipeline is consistent;
            # the model will return low-confidence nulls and flag it.
            llm_input = _IMAGE_PDF_PLACEHOLDER
        else:
            # ── Step 4: print extracted text to terminal ──────────────
            logger.info("[TEXT]  Extracted content from '%s':", path.name)
            print("\n" + "-" * 60)
            print(f"  EXTRACTED TEXT -- {path.name}")
            print("-" * 60)
            print(result.text)
            print("-" * 60 + "\n")
            llm_input = result.text

        # ── Step 5: LLM field extraction ──────────────────────────────
        logger.info("[LLM]  Sending text to LLM for field extraction...")
        invoice = self._extractor.extract(llm_input, filename=path.name)

        if invoice is None:
            reason = "LLM field extraction failed -- no structured data could be produced."
            logger.error("[LLM]  Field extraction FAILED for '%s'. Manual review required.", path.name)
            self._flag_for_review(path.name, Decision.EXTRACTION_FAILED, reason, invoice=None)
            return

        # ── Step 6: deterministic arithmetic validation ────────────────
        # Independent of the LLM -- catches "valid JSON, wrong numbers"
        # extraction errors. Always logs every check it runs/skips.
        math_result = validate_invoice_math(invoice, filename=path.name)
        if math_result.is_fully_verified:
            math_status = "verified"
        elif math_result.has_mismatch:
            math_status = "MISMATCH"
        else:
            math_status = "incomplete"

        logger.info(
            "[DONE]  Extraction finished for '%s' -- %d page(s), %d table(s), "
            "confidence=%.2f, math=%s.",
            path.name,
            result.page_count,
            result.table_count,
            invoice.extraction_confidence if invoice.extraction_confidence is not None else -1.0,
            math_status,
        )

        # ── Step 7: completeness / name-matching / duplicate checks ────
        outcome = validation_service.validate(invoice)
        logger.info(
            "[VALIDATE]  '%s' -> %s (%s)", path.name, outcome.decision.value, outcome.reason,
        )

        if outcome.decision != Decision.POSTED:
            self._flag_for_review(path.name, outcome.decision, outcome.reason, invoice=invoice)
            return

        # All of completeness / approved-supplier / duplicate passed --
        # the one remaining gate is the math check computed in step 6.
        if math_result.has_mismatch:
            reason = f"Arithmetic does not reconcile: {_math_mismatch_summary(math_result)}"
            self._flag_for_review(path.name, Decision.MATH_MISMATCH, reason, invoice=invoice)
            return

        # ── Step 8: push to Odoo ────────────────────────────────────────
        try:
            move_id = odoo_service.post_invoice(invoice, source_file=path.name)
        except Exception as exc:  # noqa: BLE001 -- never let an Odoo failure crash the watcher
            reason = f"Odoo posting failed: {exc}"
            logger.exception("[ODOO]  Failed to post '%s' to Odoo.", path.name)
            self._flag_for_review(path.name, Decision.ODOO_ERROR, reason, invoice=invoice)
            return

        invoice_repo.record_processed(
            outcome.normalized_vendor, invoice.invoice_number, path.name, move_id,
        )
        audit_log.write(
            path.name, Decision.POSTED.value, outcome.reason, detail=f"odoo_move_id={move_id}",
        )

        # ── Step 9: only successfully-posted PDFs move out of inbox ────
        self._move_to_processed(path)

        logger.info("[COMPLETE]  '%s' posted to Odoo (move #%d) and moved to processed/.", path.name, move_id)

    # ── watchdog event hooks ──────────────────────────────────────────

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._process(Path(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:
        """Fires when a file is moved/renamed *into* the watched folder."""
        if event.is_directory:
            return
        self._process(Path(event.dest_path))


class InboxWatcher:
    """Wraps watchdog Observer lifecycle for clean start/stop."""

    def __init__(self, inbox_dir: Path) -> None:
        self.inbox_dir = inbox_dir
        self._observer: Observer | None = None

    def start(self) -> None:
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        init_db()  # creates processed_invoices / review_queue / audit_log tables if absent
        # Create extractor once -- keeps the model warm in VRAM across files
        extractor = OllamaExtractor()
        handler = InboxEventHandler(extractor=extractor)
        self._observer = Observer()
        self._observer.schedule(handler, str(self.inbox_dir), recursive=False)
        self._observer.start()
        logger.info("[WATCH]  Watching inbox: %s", self.inbox_dir.resolve())

    def stop(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join()
            logger.info("Watcher stopped.")

    def run_forever(self) -> None:
        """Block the calling thread, watching until KeyboardInterrupt."""
        self.start()
        logger.info("Agent is running. Drop a PDF into '%s' to process it.", self.inbox_dir)
        logger.info("Press Ctrl+C to stop.\n")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Shutdown signal received.")
        finally:
            self.stop()
