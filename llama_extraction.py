"""Public pipeline entry point, replacing the legacy lease extraction flow."""
from pathlib import Path

from llama_parse import run_parse
from reviewer.rules import review


def run_extraction(source_pdf: str | Path, page_count: int):
    return review(run_parse(source_pdf, page_count), page_count)
