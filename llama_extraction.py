"""Public pipeline entry point, replacing the legacy lease extraction flow."""
from pathlib import Path

from llama_parse import run_parse
from reviewer.rules import review


class RulesError(RuntimeError):
    """A defect in the local form checks, not a LlamaParse failure."""


def run_extraction(source_pdf: str | Path, page_count: int):
    pages = run_parse(source_pdf, page_count)
    try:
        return review(pages, page_count)
    except Exception as exc:
        raise RulesError("The form checks failed to run.") from exc
