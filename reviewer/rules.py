"""Deterministic checks for the challenge's form-local requirements."""
import re
from datetime import datetime

from reviewer.models import Cell, Issue, ParsedPage, ReviewReport

SECTIONS = {
    'mp_header': 'Top of form', 'mp_operations': 'Operations Manager Review',
    'mp_production': 'Processing Instructions', 'qs_reviews': 'Reviewed By/Date',
    'qs_inc': 'Technical and Quality Review Elements — item 10',
    'lot_items': 'Item', 'lot_regenmed': 'RegenMed Item',
    'lot_sterilization': 'Item — sterilization', 'lot_packaging': 'Packaging',
}
FIELDS = {
    'value': 'Value', 'produced': '# Produced', 'packaged': '# Packaged',
    'technical': 'Technical', 'quality': 'Quality', 'inc': 'INC #', 'status': 'Status',
    'lot_number': 'Lot Number', 'expiration_date': 'Exp. Date',
    'manufacturer': 'Manufacturer', 'lot': 'Lot', 'quantity': 'Qty Used',
    'load_number': 'Load #', 'sterilization_date': 'Sterilization Date',
}
MP_HEADER = {
    'donor_number', 'verified_by', 'cross_reference', 'donor_sex', 'donor_age',
    'recovery_date', 'instruction_verification', 'processing_date',
    'clean_room_review', 'tissue_checked_in',
}
EXPECTED = {
    'MP-F-023': {'mp_header', 'mp_operations', 'mp_production'},
    'QS-F-049': {'qs_reviews', 'qs_inc'},
    'Lot Logs': {'lot_items', 'lot_regenmed', 'lot_sterilization', 'lot_packaging'},
}


def is_na(value: str | None) -> bool:
    return bool(re.fullmatch(r'n\s*/\s*a', (value or '').strip(), re.I))


def is_blank(value: str | None) -> bool:
    return not (value or '').strip() or bool(re.fullmatch(r'[_\s]+', value or ''))


def identify(text: str) -> str:
    text = text.lower().replace('–', '-').replace('—', '-')
    matches = set()
    if re.search(r'\bmp\s*-?\s*f\s*-?\s*023\b', text) or 'tissue open checklist' in text:
        matches.add('MP-F-023')
    if re.search(r'\bqs\s*-?\s*f\s*-?\s*049\b', text) or ('review and disposition statement' in text):
        matches.add('QS-F-049')
    if re.search(r'\bmp\s*-?\s*f\s*-?\s*021\b', text) or re.search(r'\blot\s+logs?\b', text):
        matches.add('Lot Logs')
    return next(iter(matches)) if len(matches) == 1 else 'Unknown'


class Checker:
    def __init__(self):
        self.issues = []
        self.checks = 0

    def add(self, rule, page, section, row, field, message, observed=None, manual=False):
        self.issues.append(Issue(
            rule=rule, severity='manual_review' if manual else 'error', page=page,
            section=SECTIONS.get(section, section), row=row, field=FIELDS.get(field, field),
            message=message, observed=observed,
        ))

    def required(self, cell, page, section, row, field, *, shading=False):
        self.checks += 1
        if cell is None:
            self.add('extraction.missing_cell', page, section, row, field,
                     'This cell was not extracted. Inspect the original form.', manual=True)
            return False
        if shading and cell.shaded is None:
            self.add('extraction.shading', page, section, row, field,
                     'Cannot determine whether this field is white or shaded.', cell.text, True)
            return False
        if shading and cell.shaded:
            return False
        if cell.state == 'uncertain' or cell.text.strip().lower() in {'[illegible]', '[unclear]', '?', '??'}:
            self.add('extraction.uncertain', page, section, row, field,
                     'The entry is unreadable or ambiguous; verify it on the form.', cell.text, True)
            return False
        if re.fullmatch(r'[-—–"\s]+', cell.text) and cell.text.strip():
            self.add('extraction.mark', page, section, row, field,
                     'A dash, strike-through, or ditto mark is not an explicit entry. Verify the field.', cell.text, True)
            return False
        if section.startswith('lot_') and cell.text.strip().lower() in {'not used', 'unused'}:
            self.add(section + '.na', page, section, row, field,
                     'An unused item must explicitly say N/A.', cell.text)
            return False
        if cell.state == 'blank' or is_blank(cell.text):
            self.add(section + '.required', page, section, row, field,
                     'Required field is blank. Enter a value or explicit N/A where applicable.', cell.text)
            return False
        return True

    def by_date(self, cell, page, section, row, field, *, allow_na=False, strict_date=False):
        if not self.required(cell, page, section, row, field):
            return
        if allow_na and is_na(cell.text):
            return
        if allow_na and cell.text.strip().upper() in {'NA', 'N.A.', 'N.A'}:
            self.add('qs_reviews.na_ambiguous', page, section, row, field,
                     'Verify that this entry explicitly says N/A, or supply initials and a date.', cell.text, True)
            return
        self.checks += 2
        if is_blank(cell.initials) or is_na(cell.initials):
            self.add(section + '.initials', page, section, row, field,
                     'Initials are missing; both initials and a date are required.', cell.text)
        if is_blank(cell.date) or is_na(cell.date):
            self.add(section + '.date', page, section, row, field,
                     'Date is missing; both initials and a date are required.', cell.text)
        elif strict_date and cell.date.strip() not in cell.text:
            self.add('extraction.date_evidence', page, section, row, field,
                     'The extracted date does not match the raw entry. Verify its original format.', cell.text, True)
        elif strict_date:
            valid = bool(re.fullmatch(r'\d{2}/\d{2}/\d{2}', cell.date.strip()))
            if valid:
                try:
                    datetime.strptime(cell.date.strip(), '%m/%d/%y')
                except ValueError:
                    valid = False
            if not valid:
                self.add('qs_reviews.date_format', page, section, row, field,
                         'Date must be a valid calendar date in MM/DD/YY format.', cell.date)


def review(pages: list[ParsedPage], page_count: int) -> ReviewReport:
    c = Checker()
    found = {identify(p.extraction.identification_text) for p in pages}
    supported = found - {'Unknown'}
    form = next(iter(supported)) if len(supported) == 1 else 'Unknown'
    if form == 'Unknown':
        c.add('classification.unsupported', None, 'Document', '', 'Form type',
              'Unable to identify one supported form. Upload a single MP-F-023, QS-F-049, or Lot Log.', manual=True)
    if len(pages) != page_count or {p.page for p in pages} != set(range(1, page_count + 1)):
        c.add('extraction.pages', None, 'Document', '', 'Pages', 'Not every PDF page was transcribed.', manual=True)
    present = set()
    section_locations = {}
    for p in pages:
        data = p.extraction
        detected = identify(data.identification_text)
        if detected != form or data.form_type != detected:
            c.add('classification.uncertain', p.page, 'Document', '', 'Form type',
                  'This page could not be consistently identified as the same form.', data.identification_text, True)
        if not data.complete or data.uncertainties:
            c.add('extraction.incomplete', p.page, 'Document', '', 'Transcription',
                  'Extraction needs verification: ' + ('; '.join(data.uncertainties) or 'some content is unreadable or missing.'), manual=True)
        seen = set()
        for section in data.sections:
            key = section.key
            if key not in EXPECTED.get(form, set()):
                c.add('extraction.unexpected_section', p.page, key, '', '', 'Unexpected section for this form.', manual=True)
                continue
            present.add(key)
            section_locations.setdefault(key, []).append(p.page)
            if key in seen:
                c.add('extraction.duplicate_section', p.page, key, '', '', 'Section was extracted more than once.', manual=True)
            seen.add(key)
            if not section.rows or len(section.rows) != section.listed_row_count:
                c.add('extraction.row_coverage', p.page, key, '', '', 'Listed row coverage is incomplete; check this section.', manual=True)
            keys = [r.key for r in section.rows]
            if len(set(keys)) != len(keys):
                c.add('extraction.duplicate_row', p.page, key, '', '', 'Duplicate row references need verification.', manual=True)
            required_keys = MP_HEADER if key == 'mp_header' else ({str(i) for i in range(1, 11)} if key == 'qs_reviews' else set())
            for missing in sorted(required_keys - set(keys)):
                c.add('extraction.missing_row', p.page, key, missing.replace('_', ' '), '', 'Expected field or row was not extracted.', manual=True)
            for row in section.rows:
                cells, label = row.cells, row.label
                if key == 'qs_reviews':
                    if len(label) > 100:
                        label = label[:100].rsplit(' ', 1)[0] + '…'
                    label = f'Item {row.key}: {label}'
                if key in {'mp_header', 'mp_operations'}:
                    cell = cells.get('value')
                    by_date = key == 'mp_operations' or row.key in {'clean_room_review', 'tissue_checked_in'} or bool(re.search(r'by\s*/\s*date', label, re.I))
                    if by_date:
                        c.by_date(cell, p.page, key, label, 'value')
                    else:
                        c.required(cell, p.page, key, label, 'value')
                elif key == 'mp_production':
                    for field in ['produced', 'packaged']:
                        c.required(cells.get(field), p.page, key, label, field, shading=True)
                elif key == 'qs_reviews':
                    for field in ['technical', 'quality']:
                        c.by_date(cells.get(field), p.page, key, label, field, allow_na=True, strict_date=True)
                elif key == 'qs_inc':
                    inc = cells.get('inc')
                    c.checks += 1
                    if inc is None or inc.state == 'uncertain':
                        c.add('extraction.inc', p.page, key, label, 'inc', 'Cannot determine whether an INC # is entered.', manual=True)
                    elif inc.state != 'blank' and not is_blank(inc.text) and not is_na(inc.text):
                        c.required(cells.get('status'), p.page, key, label, 'status')
                else:
                    required = {
                        'lot_items': ['lot_number', 'expiration_date', 'manufacturer'],
                        'lot_regenmed': ['lot', 'quantity'],
                        'lot_sterilization': ['load_number', 'sterilization_date'],
                        'lot_packaging': ['lot', 'quantity'],
                    }[key]
                    for field in required:
                        c.required(cells.get(field), p.page, key, label, field)
    for missing in sorted(EXPECTED.get(form, set()) - present):
        c.add('extraction.missing_section', None, missing, '', '', 'Required section is missing or was not extracted.', manual=True)
    if form == 'Lot Logs':
        if page_count != 2:
            c.add('lot.pages', None, 'Document', '', 'Pages', 'The Lot Log requires both page 1 and page 2.', manual=True)
        for section, expected in [('lot_items', 1), ('lot_regenmed', 1), ('lot_sterilization', 2), ('lot_packaging', 2)]:
            if section in section_locations and section_locations[section] != [expected]:
                c.add('lot.page_layout', None, section, '', '', 'Section is not on the expected PDF page; verify page order and completeness.', manual=True)
    if form in {'MP-F-023', 'QS-F-049'} and page_count != 1:
        c.add('document.extra_pages', None, 'Document', '', 'Pages', 'Expected a single form page; inspect additional pages.', manual=True)
    errors = sum(i.severity == 'error' for i in c.issues)
    uncertain = len(c.issues) - errors
    status = 'unsupported' if form == 'Unknown' else ('needs_review' if uncertain else ('issues_found' if errors else 'passed'))
    summary = ('All applicable challenge checks passed. Staff review is still required.' if status == 'passed'
               else f'{errors} field issue(s), {uncertain} item(s) requiring manual verification. This form has not passed all checks.')
    return ReviewReport(form_type=form, status=status, summary=summary, page_count=page_count,
                        checks_run=c.checks, issues=c.issues, extracted_pages=pages)



