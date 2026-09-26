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


def parse_page_json(markdown: str) -> PageExtraction:
    text = markdown.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    if fenced:
        text = fenced.group(1)
    try:
        return PageExtraction.model_validate(json.loads(text))
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

