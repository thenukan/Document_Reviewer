"""LlamaParse v2 adapter. All form transcription is performed by LlamaParse."""
import json
import os
import re
from copy import deepcopy
from pathlib import Path

from llama_cloud import LlamaCloud
from pydantic import ValidationError

from reviewer.models import PageExtraction, ParsedPage
from reviewer.prompts import EXTRACTION_PROMPT


class ExtractionError(RuntimeError):
    def __init__(self, message, *, diagnostic=None):
        super().__init__(message)
        self.diagnostic = diagnostic


def validation_diagnostic(exc: ValidationError) -> str:
    """Report schema paths/types without logging document values or arbitrary keys."""
    schema = PageExtraction.model_json_schema()
    safe_fields = set(schema.get("properties", {}))
    for definition in schema.get("$defs", {}).values():
        safe_fields.update(definition.get("properties", {}))
    details = []
    for error in exc.errors(include_input=False, include_context=False, include_url=False)[:8]:
        path = ".".join(str(part) if isinstance(part, int) or part in safe_fields
                        else "[key]" for part in error["loc"]) or "root"
        details.append(f"{path}: {error['type']}")
    return f"schema validation ({exc.error_count()} errors): " + "; ".join(details)


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


def recover_partial_page(payload, errors):
    """Recover known metadata defects without inventing any entered values."""
    skipped = set()
    missing_states = []
    missing_labels = []
    malformed_uncertainties = False
    for error in errors:
        loc = error['loc']
        if (error['type'] == 'literal_error' and len(loc) == 3
                and loc[0] == 'sections' and isinstance(loc[1], int) and loc[2] == 'key'):
            skipped.add(loc[1])
        elif (error['type'] == 'missing' and len(loc) == 7
                and loc[0] == 'sections' and isinstance(loc[1], int)
                and loc[2] == 'rows' and isinstance(loc[3], int)
                and loc[4] == 'cells' and isinstance(loc[5], str) and loc[6] == 'state'):
            missing_states.append(loc)
        elif (len(loc) == 5 and loc[0] == 'sections' and isinstance(loc[1], int)
                and loc[2] == 'rows' and isinstance(loc[3], int) and loc[4] == 'label'
                and (error['type'] == 'missing'
                     or (error['type'] == 'string_type'
                         and payload['sections'][loc[1]]['rows'][loc[3]].get('label') is None))):
            missing_labels.append(loc)
        elif error['type'] == 'list_type' and loc == ('uncertainties',):
            malformed_uncertainties = True
        else:
            return None  # Other malformed data must still fail validation.

    partial = deepcopy(payload)
    if malformed_uncertainties:
        # These are parser notes, not form entries. Preserve the original value
        # as text rather than treating a malformed value as "no uncertainties".
        raw = partial['uncertainties']
        original = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        partial['uncertainties'] = [
            'LlamaParse returned uncertainty notes in an unexpected format. '
            'Check this page on the original PDF.',
            f'Original uncertainty notes: {original}',
        ]
    counts = {}
    for loc in missing_states:
        if loc[1] in skipped:
            continue
        cell = partial['sections'][loc[1]]['rows'][loc[3]]['cells'][loc[5]]
        cell['state'] = 'uncertain'
        counts[loc[1]] = counts.get(loc[1], 0) + 1
    label_warnings = []
    for loc in missing_labels:
        if loc[1] in skipped:
            continue
        section = partial['sections'][loc[1]]
        row = section['rows'][loc[3]]
        row['label'] = f'Row {loc[3] + 1} (label unavailable)'
        warning = (f'Extracted section {loc[1] + 1}, row {loc[3] + 1}: the row label was missing '
                   'or null. A display placeholder is used; extracted values are preserved. '
                   'Check this row on the original PDF.')
        if section['key'] == 'mp_header' and row['key'] not in {'clean_room_review', 'tissue_checked_in'}:
            # Some header By/Date checks depend on the printed label. Retain
            # text and components, but do not issue a presence pass for this cell.
            if 'value' in row['cells']:
                row['cells']['value']['state'] = 'uncertain'
            warning += ' The header entry is uncertain because its label determines which checks apply.'
        label_warnings.append(warning)
    partial['sections'] = [section for index, section in enumerate(partial['sections'])
                           if index not in skipped]
    partial['complete'] = False
    partial['uncertainties'] = list(partial['uncertainties']) + [
        f'Skipped extracted section {index + 1} because its section name was not recognized. '
        'Check this section on the original PDF.' for index in sorted(skipped)
    ] + [
        f'Extracted section {index + 1}: LlamaParse omitted the filled/blank/uncertain state '
        f'for {count} cell(s). Their text is preserved, but the entries require manual verification.'
        for index, count in sorted(counts.items())
    ] + label_warnings
    return PageExtraction.model_validate(partial)


def parse_page_json(markdown: str) -> PageExtraction:
    text = markdown.strip()
    if not text:
        raise ExtractionError(
            "LlamaParse returned no transcription for this page. "
            "Retry the document; it has not passed review.", diagnostic="empty transcription"
        )
    fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    if fenced:
        text = fenced.group(1)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExtractionError(
            "LlamaParse returned a transcription that is not valid JSON. "
            "Retry the document; it has not passed review.",
            diagnostic=f"invalid JSON at line {exc.lineno}, column {exc.colno}",
        ) from exc
    payload = normalize_envelope(payload)
    try:
        return PageExtraction.model_validate(payload)
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_context=False, include_url=False)
        partial = recover_partial_page(payload, errors)
        if partial is not None:
            return partial
        raise ExtractionError(
            "LlamaParse returned JSON that does not match the required form structure. "
            "Retry the document; if this repeats, check the server logs for schema errors. "
            "The document has not passed review.", diagnostic=validation_diagnostic(exc),
        ) from exc


def parse_timeout():
    try:
        value = float(os.getenv("LLAMA_PARSE_TIMEOUT", "300"))
    except ValueError:
        return None
    return value if value > 0 else None


def config_problems() -> list[str]:
    """Parser settings that would make every review fail; checked before accepting uploads."""
    problems = []
    if os.getenv("LLAMA_PARSE_TIER", "agentic") not in {"agentic", "agentic_plus"}:
        problems.append("LLAMA_PARSE_TIER must be agentic or agentic_plus.")
    if parse_timeout() is None:
        problems.append("LLAMA_PARSE_TIMEOUT must be a positive number of seconds.")
    return problems


def run_parse(source_pdf: str | Path, page_count: int) -> list[ParsedPage]:
    key = os.getenv("LLAMA_CLOUD_API_KEY") or os.getenv("LLAMA_PARSE_API_KEY")
    if not key:
        raise ExtractionError("Set LLAMA_CLOUD_API_KEY or LLAMA_PARSE_API_KEY on the server.")
    if problems := config_problems():
        raise ExtractionError(" ".join(problems))
    tier = os.getenv("LLAMA_PARSE_TIER", "agentic")
    with LlamaCloud(api_key=key, timeout=60, max_retries=2) as client:
        result = client.parsing.parse(
            upload_file=Path(source_pdf), tier=tier,
            version=os.getenv("LLAMA_PARSE_VERSION", "latest"),
            agentic_options={"custom_prompt": EXTRACTION_PROMPT},
            expand=["markdown"],
            output_options={"markdown": {"tables": {"merge_continued_tables": False}}},
            processing_control={"job_failure_conditions": {"allowed_page_failure_ratio": 0.001}},
            timeout=parse_timeout(),
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
        try:
            extraction = parse_page_json(page.get("markdown") or "")
        except ExtractionError as exc:
            raise ExtractionError(f"PDF page {number}: {exc}",
                                  diagnostic=f"page {number}: {exc.diagnostic}") from exc
        parsed.append(ParsedPage(page=number, extraction=extraction))
    if len({p.page for p in parsed}) != page_count:
        raise ExtractionError("LlamaParse returned duplicate or missing pages.")
    return sorted(parsed, key=lambda p: p.page)
