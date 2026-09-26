"""Bonus-round checks; each Discard Form page is reviewed independently."""
import re

DISCARD_SECTIONS = {'discard_header', 'discard_status', 'discard_tissues', 'discard_bottom'}
HEADER_FIELDS = {
    'donor_number': 'Donor #',
    'authorized_by_date': 'Discard Authorized By/Date',
    'reason_for_discard': 'Reason for Discard',
}
STATUS_FIELDS = {
    'unprocessed': 'Unprocessed Tissue',
    'in_processing': 'In Processing Tissue',
    'unreleased_packaged': 'Unreleased Packaged Tissue',
    'released_packaged': 'Released Packaged Tissue',
}
BOTTOM_FIELDS = {
    'tissue_discarded_by': 'Tissue Discarded By',
    'confirmed_by': 'Confirmed By',
    'discard_date': 'Discard confirmation — Date',
    'distribution_updated_by': 'Distribution — FreezerPro Updated By',
    'distribution_updated_date': 'Distribution — Date',
    'donor_chart_updated_by': 'Donor Chart — Log / FreezerPro Updated By',
    'donor_chart_updated_date': 'Donor Chart — Date',
}
PACKAGED = {'unreleased_packaged', 'released_packaged'}
UNPACKAGED = {'unprocessed', 'in_processing'}


def checkbox(c, cell, page, section, row, field):
    """Return an observed checkbox state; a missing/ambiguous state never passes."""
    c.checks += 1
    unknown = cell is None or cell.state == 'uncertain' or cell.checked is None
    if not unknown:
        text = cell.text.strip()
        if cell.checked:
            unknown = cell.state != 'filled' or text.lower() not in {'x', '✓', '✔', '☑', '☒', '[x]', '[✓]', 'checkmark', 'check', 'checked', 'cross'}
        else:
            unknown = bool(text) and text.lower() not in {'☐', '□', '[ ]', 'unchecked', 'empty'}
    if unknown:
        c.add('extraction.checkbox', page, section, row, field,
              'The checkbox mark could not be verified. Inspect the original form.',
              cell.text if cell else None, True)
        return None
    return cell.checked


def graft_kind(c, cell, page, label):
    c.checks += 1
    text = cell.text.strip() if cell else ''
    if (cell is None or cell.state != 'filled' or not text
            or not re.search(r'[A-Za-z0-9]', text)
            or text.lower() in {'na', 'n.a.', 'n.a', 'none', 'unknown', 'illegible', '[illegible]', '[unclear]', 'not applicable'}):
        c.add('discard_tissues.graft_uncertain', page, 'discard_tissues', label, 'graft_id',
              'Cannot determine whether this row contains a Graft ID or explicit N/A. Verify the entry before checking Tissue Status.',
              text, True)
        return None
    return 'na' if re.fullmatch(r'n\s*/\s*a', text, re.I) else 'id'


def review_discard_page(c, parsed):
    page = parsed.page
    sections = parsed.extraction.sections
    present = {s.key for s in sections}
    for missing in sorted(DISCARD_SECTIONS - present):
        c.add('extraction.missing_section', page, missing, '', '',
              'Required section is missing or was not extracted on this Discard Form page.', manual=True)

    seen = set()
    status_cells = {}
    tissues = []
    for section in sections:
        key = section.key
        if key not in DISCARD_SECTIONS:
            c.add('extraction.unexpected_section', page, key, '', '', 'Unexpected section for a Discard Form.', manual=True)
            continue
        if key in seen:
            c.add('extraction.duplicate_section', page, key, '', '', 'Section was extracted more than once.', manual=True)
        seen.add(key)
        if len(section.rows) != section.listed_row_count or (not section.rows and key != 'discard_tissues'):
            c.add('extraction.row_coverage', page, key, '', '', 'Listed row coverage is incomplete; check this section.', manual=True)
        keys = [r.key for r in section.rows]
        if len(keys) != len(set(keys)):
            c.add('extraction.duplicate_row', page, key, '', '', 'Duplicate row references need verification.', manual=True)
        expected = {'discard_header': HEADER_FIELDS, 'discard_status': STATUS_FIELDS,
                    'discard_bottom': BOTTOM_FIELDS}.get(key, {})
        for missing in sorted(set(expected) - set(keys)):
            c.add('extraction.missing_row', page, key, expected[missing], '',
                  'Expected field was not extracted on this page.', manual=True)
        for row in section.rows:
            label = expected.get(row.key, row.label)
            if key == 'discard_header':
                if row.key == 'authorized_by_date':
                    c.by_date(row.cells.get('value'), page, key, label, 'value')
                else:
                    c.required(row.cells.get('value'), page, key, label, 'value')
            elif key == 'discard_bottom':
                c.required(row.cells.get('value'), page, key, label, 'value')
            elif key == 'discard_status':
                if row.key not in STATUS_FIELDS:
                    c.add('extraction.status_option', page, key, label, 'value',
                          'Unrecognized Tissue Status option; verify the form layout.', manual=True)
                state = checkbox(c, row.cells.get('value'), page, key, label, 'value')
                status_cells[row.key] = state
            elif key == 'discard_tissues':
                confirmed = checkbox(c, row.cells.get('confirmation_x'), page, key, row.label, 'confirmation_x')
                if confirmed is False:
                    c.add('discard_tissues.confirmation', page, key, row.label, 'confirmation_x',
                          'Complete the small X confirmation box for this listed tissue.', '')
                tissues.append((row, graft_kind(c, row.cells.get('graft_id'), page, row.label)))

    c.checks += 1
    selected = [key for key, checked in status_cells.items() if checked is True]
    status_known = set(status_cells) == set(STATUS_FIELDS) and all(s is not None for s in status_cells.values())
    if not selected and status_known:
        c.add('discard_status.required', page, 'discard_status', '', 'Tissue Status',
              'Check one Tissue Status box; none is selected.', '')
    elif len(selected) > 1:
        c.add('discard_status.multiple', page, 'discard_status', '', 'Tissue Status',
              'Select one Tissue Status. Multiple boxes are checked.',
              ', '.join(STATUS_FIELDS.get(key, key) for key in selected))
    if not status_known or len(selected) != 1:
        return
    chosen = selected[0]
    for row, kind in tissues:
        if kind is None:
            continue
        c.checks += 1
        allowed = PACKAGED if kind == 'id' else UNPACKAGED
        if chosen not in allowed:
            required = 'Unreleased Packaged Tissue or Released Packaged Tissue' if kind == 'id' else 'Unprocessed Tissue or In Processing Tissue'
            reason = 'A Graft ID is listed' if kind == 'id' else 'The Graft ID is N/A'
            c.add('discard_status.graft_mismatch', page, 'discard_tissues', row.label, 'graft_id',
                  f'{reason}; Tissue Status must be {required}.',
                  f'{row.cells["graft_id"].text}; checked: {STATUS_FIELDS[chosen]}')
