"""LlamaParse v2 adapter. All form transcription is performed by LlamaParse."""
import json
import os
import re
from pathlib import Path

from llama_cloud import LlamaCloud
from pydantic import ValidationError

from reviewer.models import PageExtraction, ParsedPage
from reviewer.prompts import EXTRACTION_PROMPT


class ExtractionError(RuntimeError):
    pass


def normalize_envelope(payload):
    """Normalize parser metadata only; never repair entered values or missing cells.

    PDF page locations come from the SDK, not the printed footer. LlamaParse can
    return a full title as the type or a form revision in place of a page number.
    """
    if not isinstance(payload, dict):
        return payload
    payload = dict(payload)
    form = payload.get("form_type")
    if isinstance(form, str) and ":" in form:
        prefix = form.split(":", 1)[0].strip()
        if prefix in {"MP-F-023", "QS-F-049", "Lot Logs", "Discard Form"}:
            payload["form_type"] = prefix
    printed = payload.get("printed_page")
    if isinstance(printed, str):
        number = re.fullmatch(r"(?:Page\s+)?(\d+)(?:\s+of\s+\d+)?", printed.strip(), re.I)
        if number:
            payload["printed_page"] = int(number.group(1))
        elif re.fullmatch(r"[A-Z]{2}-F-\d{3}(?:\.\d+)?", printed.strip(), re.I):
            payload["printed_page"] = None  # A form revision is not a printed page number.
    if payload.get("form_type") == "QS-F-049" and isinstance(payload.get("sections"), list):
        # These two non-review sections are explicitly outside the QS challenge
        # rules. Expected qs_reviews/qs_inc coverage is still checked separately.
        payload["sections"] = [s for s in payload["sections"]
                               if not isinstance(s, dict) or s.get("key") not in {"qs_header", "qs_disposition"}]
    return payload


def parse_page_json(markdown: str) -> PageExtraction:
    text = markdown.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    if fenced:
        text = fenced.group(1)
    try:
        return PageExtraction.model_validate(normalize_envelope(json.loads(text)))
    except (ValueError, ValidationError) as exc:
        raise ExtractionError(
            "LlamaParse returned incomplete or unstructured transcription. "
            "Retry with a clearer scan; this document has not passed review."
        ) from exc


def run_parse(source_pdf: str | Path, page_count: int) -> list[ParsedPage]:
    key = os.getenv("LLAMA_CLOUD_API_KEY") or os.getenv("LLAMA_PARSE_API_KEY")
    if not key:
        raise ExtractionError("Set LLAMA_CLOUD_API_KEY or LLAMA_PARSE_API_KEY on the server.")
    tier = os.getenv("LLAMA_PARSE_TIER", "agentic")
    if tier not in {"agentic", "agentic_plus"}:
        raise ExtractionError("LLAMA_PARSE_TIER must be agentic or agentic_plus.")
    with LlamaCloud(api_key=key, timeout=60, max_retries=2) as client:
        result = client.parsing.parse(
            upload_file=Path(source_pdf), tier=tier,
            version=os.getenv("LLAMA_PARSE_VERSION", "latest"),
            agentic_options={"custom_prompt": EXTRACTION_PROMPT},
            expand=["markdown"],
            output_options={"markdown": {"tables": {"merge_continued_tables": False}}},
            processing_control={"job_failure_conditions": {"allowed_page_failure_ratio": 0.001}},
            timeout=float(os.getenv("LLAMA_PARSE_TIMEOUT", "300")),
        )
        payload = result.model_dump(mode="json")
    if payload.get("job", {}).get("status") != "COMPLETED":
        raise ExtractionError("LlamaParse did not complete. No review result is available.")
    pages = (payload.get("markdown") or {}).get("pages") or []
    if len(pages) != page_count:
        raise ExtractionError("LlamaParse did not return every PDF page. No pass can be issued.")
    parsed = []
    for page in pages:
        if page.get('success') is False:
            raise ExtractionError('LlamaParse could not transcribe every PDF page.')
        number = page.get("page_number")
        if not isinstance(number, int) or not 1 <= number <= page_count:
            raise ExtractionError("LlamaParse returned invalid page references.")
        parsed.append(ParsedPage(page=number, extraction=parse_page_json(page.get("markdown") or "")))
    if len({p.page for p in parsed}) != page_count:
        raise ExtractionError("LlamaParse returned duplicate or missing pages.")
    return sorted(parsed, key=lambda p: p.page)

