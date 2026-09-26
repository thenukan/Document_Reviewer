from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Cell(StrictModel):
    text: str
    state: Literal["filled", "blank", "uncertain"]
    shaded: bool | None = False
    initials: str | None = None
    date: str | None = None


class Row(StrictModel):
    key: str
    label: str
    cells: dict[str, Cell]


class Section(StrictModel):
    key: Literal[
        "mp_header", "mp_operations", "mp_production", "qs_reviews", "qs_inc",
        "lot_items", "lot_regenmed", "lot_sterilization", "lot_packaging",
    ]
    listed_row_count: int = Field(ge=0)
    rows: list[Row]


class PageExtraction(StrictModel):
    form_type: Literal["MP-F-023", "QS-F-049", "Lot Logs", "Unknown"]
    identification_text: str
    printed_page: int | None = Field(default=None, ge=1)
    complete: bool
    uncertainties: list[str]
    sections: list[Section]


class ParsedPage(StrictModel):
    page: int = Field(ge=1)
    extraction: PageExtraction


class Issue(StrictModel):
    rule: str
    severity: Literal["error", "manual_review"]
    page: int | None
    section: str
    row: str
    field: str
    message: str
    observed: str | None = None


class ReviewReport(StrictModel):
    form_type: str
    status: Literal["passed", "issues_found", "needs_review", "unsupported"]
    summary: str
    page_count: int
    checks_run: int
    issues: list[Issue]
    extracted_pages: list[ParsedPage]
    rules_version: str = "regenmed-hackathon-1"
    scope: str = "Pre-review only. The required two-person staff review remains in place."
