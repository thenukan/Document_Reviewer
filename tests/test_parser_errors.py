import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from llama_parse import ExtractionError, parse_page_json, run_parse
from reviewer.models import ParsedPage
from reviewer.rules import review
from test_rules import qs, mp, lot


def mp_missing_states():
    """An MP production table with 85 missing cell states."""
    payload = mp()[0].extraction.model_dump()
    section = payload['sections'][2]
    section['rows'] = [
        {'key': str(index), 'label': f'Tissue {index}', 'cells': {
            name: {'text': text, 'shaded': False}
            for name, text in [('produced', '0'), ('packaged', '2'), ('extra_a', 'N/A'),
                               ('extra_b', ''), ('extra_c', '04-23-26')]
        }} for index in range(17)
    ]
    section['listed_row_count'] = 17
    return payload


class ParserErrorTests(unittest.TestCase):
    def test_empty_and_malformed_output_have_distinct_diagnostics(self):
        for text, message, diagnostic in [
            ('  ', 'no transcription', 'empty transcription'),
            ('private-document-text', 'not valid JSON', 'invalid JSON at line 1, column 1'),
            ('```json\n{"form_type":\n```', 'not valid JSON', 'invalid JSON at line'),
        ]:
            with self.subTest(text=text), self.assertRaises(ExtractionError) as caught:
                parse_page_json(text)
            self.assertIn(message, str(caught.exception))
            self.assertIn(diagnostic, caught.exception.diagnostic)
            self.assertNotIn('private-document-text', caught.exception.diagnostic)

    def test_schema_diagnostic_redacts_values_and_arbitrary_keys(self):
        payload = qs()[0].extraction.model_dump()
        del payload['form_type']
        payload['private-extra-key'] = 'secret-value'
        payload['sections'][0]['rows'][0]['cells']['private-cell-key'] = {
            'text': 'private-cell-text', 'state': 'private-invalid-state'
        }
        with self.assertRaises(ExtractionError) as caught:
            parse_page_json(json.dumps(payload))
        error = caught.exception
        self.assertIn('required form structure', str(error))
        self.assertIn('form_type: missing', error.diagnostic)
        self.assertIn('sections.0.rows.0.cells.[key].state: literal_error', error.diagnostic)
        for private in ['private-extra-key', 'secret-value', 'private-cell-key',
                        'private-cell-text', 'private-invalid-state']:
            self.assertNotIn(private, str(error) + error.diagnostic)

    def test_page_number_is_in_error_and_diagnostic(self):
        payload = {'job': {'status': 'COMPLETED'}, 'markdown': {'pages': [
            {'page_number': 1, 'markdown': qs()[0].extraction.model_dump_json()},
            {'page_number': 2, 'markdown': '{}'},
        ]}}
        with patch('llama_parse.LlamaCloud') as cls, \
                patch.dict(os.environ, {'LLAMA_PARSE_API_KEY': 'test-only'}):
            cls.return_value.__enter__.return_value.parsing.parse.return_value.model_dump.return_value = payload
            with self.assertRaises(ExtractionError) as caught:
                run_parse(Path('unseen.pdf'), 2)
        self.assertIn('PDF page 2', str(caught.exception))
        self.assertIn('page 2: schema validation', caught.exception.diagnostic)

    def test_incomplete_valid_transcription_remains_available_for_review(self):
        payload = qs()[0].extraction.model_dump()
        payload['complete'] = False
        payload['uncertainties'] = ['Unreadable handwriting']
        self.assertFalse(parse_page_json(json.dumps(payload)).complete)

    def test_non_list_uncertainties_preserve_notes_and_always_need_review(self):
        for raw in ['Unreadable initials; verify date \u2014 page 1', '', None, False, 0,
                    {'technical': 'Date is unclear', 'rows': [1, 2]}]:
            with self.subTest(raw=raw):
                payload = qs()[0].extraction.model_dump()
                payload['uncertainties'] = raw
                parsed = parse_page_json(json.dumps(payload))
                self.assertEqual(parsed.sections, qs()[0].extraction.sections)
                self.assertFalse(parsed.complete)
                self.assertIn('unexpected format', parsed.uncertainties[0])
                original = parsed.uncertainties[1].removeprefix('Original uncertainty notes: ')
                if isinstance(raw, str):
                    self.assertEqual(original, raw)
                else:
                    self.assertEqual(json.loads(original), raw)
                report = review([ParsedPage(page=1, extraction=parsed)], 1)
                self.assertEqual(report.status, 'needs_review')
                self.assertTrue(report.passed_checks)
                self.assertEqual(report.issues[0].rule, 'extraction.incomplete')
                self.assertEqual(report.issues[0].page, 1)

    def test_non_list_uncertainties_recover_with_unknown_sections_and_missing_states(self):
        payload = mp_missing_states()
        payload['uncertainties'] = 'Check the handwriting'
        payload['sections'].insert(0, {'key': 'unknown_header', 'listed_row_count': 0, 'rows': []})
        payload['sections'].insert(1, {'key': 'unknown_footer', 'listed_row_count': 0, 'rows': []})
        parsed = parse_page_json(json.dumps(payload))
        self.assertEqual(len(parsed.sections), 3)
        self.assertFalse(parsed.complete)
        notes = '; '.join(parsed.uncertainties)
        self.assertIn('Check the handwriting', notes)
        self.assertIn('Skipped extracted section 1', notes)
        self.assertIn('Skipped extracted section 2', notes)
        self.assertIn('85 cell(s)', notes)
        self.assertEqual(parsed.sections[2].rows[0].cells['produced'].text, '0')
        self.assertEqual(parsed.sections[2].rows[0].cells['produced'].state, 'uncertain')
        self.assertEqual(review([ParsedPage(page=1, extraction=parsed)], 1).status, 'needs_review')

    def test_non_list_uncertainties_do_not_hide_malformed_form_data(self):
        for defect in ['missing_complete', 'missing_text', 'invalid_state', 'missing_section_key',
                       'extra_field']:
            with self.subTest(defect=defect):
                payload = qs()[0].extraction.model_dump()
                payload['uncertainties'] = 'Check the handwriting'
                if defect == 'missing_complete':
                    del payload['complete']
                elif defect == 'missing_text':
                    del payload['sections'][0]['rows'][0]['cells']['technical']['text']
                elif defect == 'invalid_state':
                    payload['sections'][0]['rows'][0]['cells']['technical']['state'] = 'invalid'
                elif defect == 'missing_section_key':
                    del payload['sections'][0]['key']
                else:
                    payload['unexpected'] = 'private-value'
                with self.assertRaises(ExtractionError) as caught:
                    parse_page_json(json.dumps(payload))
                self.assertIn('uncertainties: list_type', caught.exception.diagnostic)
                self.assertNotIn('private-value', caught.exception.diagnostic)

    def test_uncertainty_arrays_still_require_string_items(self):
        payload = qs()[0].extraction.model_dump()
        payload['uncertainties'] = [None]
        with self.assertRaises(ExtractionError) as caught:
            parse_page_json(json.dumps(payload))
        self.assertIn('uncertainties.0: string_type', caught.exception.diagnostic)

    def test_missing_or_null_row_labels_preserve_values_for_every_form(self):
        from test_discard import discard
        for factory in [mp, qs, lot, lambda: [discard()]]:
            for defect in ['missing', 'null']:
                pages = factory()
                payload = pages[0].extraction.model_dump()
                payload['uncertainties'] = ['Existing warning']
                original = pages[0].extraction.sections[1].rows[0]
                row = payload['sections'][1]['rows'][0]
                if defect == 'missing':
                    del row['label']
                else:
                    row['label'] = None
                with self.subTest(form=payload['form_type'], defect=defect):
                    parsed = parse_page_json(json.dumps(payload))
                    restored = parsed.sections[1].rows[0]
                    self.assertEqual(restored.label, 'Row 1 (label unavailable)')
                    self.assertEqual(restored.key, original.key)
                    self.assertEqual(restored.cells, original.cells)
                    self.assertEqual(len(parsed.sections[1].rows), len(pages[0].extraction.sections[1].rows))
                    self.assertFalse(parsed.complete)
                    self.assertEqual(parsed.uncertainties[0], 'Existing warning')
                    self.assertIn('section 2, row 1', parsed.uncertainties[1])
                    pages[0] = ParsedPage(page=1, extraction=parsed)
                    report = review(pages, len(pages))
                    self.assertEqual(report.status, 'needs_review')
                    self.assertTrue(report.passed_checks)

    def test_missing_labels_recover_with_other_metadata_defects(self):
        payload = mp_missing_states()
        payload['uncertainties'] = 'Verify handwriting'
        del payload['sections'][1]['rows'][0]['label']
        del payload['sections'][2]['rows'][1]['label']
        payload['sections'].insert(0, {'key': 'unknown_header', 'listed_row_count': 0,
                                      'rows': [{'key': 'unknown_row', 'cells': {}}]})
        parsed = parse_page_json(json.dumps(payload))
        self.assertEqual(len(parsed.sections), 3)
        self.assertEqual(parsed.sections[1].rows[0].label, 'Row 1 (label unavailable)')
        self.assertEqual(parsed.sections[2].rows[1].label, 'Row 2 (label unavailable)')
        self.assertEqual(parsed.sections[2].rows[1].cells['produced'].text, '0')
        notes = '; '.join(parsed.uncertainties)
        self.assertIn('Verify handwriting', notes)
        self.assertIn('Skipped extracted section 1', notes)
        self.assertIn('85 cell(s)', notes)
        self.assertIn('section 3, row 1', notes)
        self.assertIn('section 4, row 2', notes)
        self.assertNotIn('section 1, row 1', notes)
        self.assertEqual(review([ParsedPage(page=1, extraction=parsed)], 1).status, 'needs_review')

    def test_missing_labels_do_not_hide_missing_or_malformed_form_data(self):
        for defect in ['text', 'cells', 'key', 'complete', 'label_object']:
            payload = qs()[0].extraction.model_dump()
            del payload['sections'][1]['rows'][0]['label']
            row = payload['sections'][0]['rows'][0]
            if defect == 'text':
                del row['cells']['technical']['text']
            elif defect in {'cells', 'key'}:
                del row[defect]
            elif defect == 'complete':
                del payload['complete']
            else:
                row['label'] = {'private-key': 'private-value'}
            with self.subTest(defect=defect), self.assertRaises(ExtractionError) as caught:
                parse_page_json(json.dumps(payload))
            self.assertIn('sections.1.rows.0.label: missing', caught.exception.diagnostic)
            self.assertNotIn('private-value', caught.exception.diagnostic)

    def test_missing_mp_header_label_does_not_overclaim_signature_checks(self):
        payload = mp()[0].extraction.model_dump()
        payload['sections'][0]['rows'].append({
            'key': 'additional_review',
            'cells': {'value': {'text': 'AB', 'state': 'filled', 'initials': 'AB', 'date': None}},
        })
        payload['sections'][0]['listed_row_count'] += 1
        parsed = parse_page_json(json.dumps(payload))
        row = parsed.sections[0].rows[-1]
        self.assertEqual(row.cells['value'].text, 'AB')
        self.assertEqual(row.cells['value'].initials, 'AB')
        self.assertIsNone(row.cells['value'].date)
        self.assertEqual(row.cells['value'].state, 'uncertain')
        report = review([ParsedPage(page=1, extraction=parsed)], 1)
        self.assertEqual(report.status, 'needs_review')
        self.assertFalse(any(check.row == row.label for check in report.passed_checks))
        self.assertTrue(any(issue.rule == 'extraction.uncertain' and issue.row == row.label
                            for issue in report.issues))

    def test_missing_mp_signature_label_keeps_checks_determined_by_key(self):
        payload = mp()[0].extraction.model_dump()
        for row in payload['sections'][0]['rows']:
            if row['key'] == 'tissue_checked_in':
                del row['label']
                row['cells']['value']['date'] = None
        parsed = parse_page_json(json.dumps(payload))
        report = review([ParsedPage(page=1, extraction=parsed)], 1)
        self.assertEqual(report.status, 'needs_review')
        self.assertIn('mp_header.date', [issue.rule for issue in report.issues])

    def test_unknown_sections_are_skipped_without_losing_valid_details(self):
        payload = qs()[0].extraction.model_dump()
        original = qs()[0].extraction.sections
        payload['sections'].insert(0, {'key': 'unknown_header', 'listed_row_count': 0, 'rows': []})
        payload['sections'].append({'key': 'unknown_footer', 'listed_row_count': 0, 'rows': []})
        payload['uncertainties'] = ['Existing uncertainty']
        parsed = parse_page_json(json.dumps(payload))
        self.assertEqual(parsed.sections, original)
        self.assertFalse(parsed.complete)
        self.assertEqual(parsed.uncertainties[0], 'Existing uncertainty')
        self.assertIn('section 1', parsed.uncertainties[1])
        self.assertIn('section 4', parsed.uncertainties[2])
        report = review([ParsedPage(page=1, extraction=parsed)], 1)
        self.assertEqual(report.status, 'needs_review')
        self.assertTrue(report.passed_checks)
        self.assertEqual(report.issues[0].rule, 'extraction.incomplete')
        self.assertEqual(report.issues[0].page, 1)

    def test_skipping_misnamed_required_section_still_flags_missing_coverage(self):
        payload = qs()[0].extraction.model_dump()
        payload['sections'][0]['key'] = 'review_elements'
        parsed = parse_page_json(json.dumps(payload))
        report = review([ParsedPage(page=1, extraction=parsed)], 1)
        self.assertEqual(report.status, 'needs_review')
        self.assertIn('extraction.missing_section', [issue.rule for issue in report.issues])

    def test_all_sections_skipped_cannot_pass(self):
        payload = qs()[0].extraction.model_dump()
        for section in payload['sections']:
            section['key'] = 'unknown_section'
        parsed = parse_page_json(json.dumps(payload))
        self.assertEqual(parsed.sections, [])
        self.assertEqual(review([ParsedPage(page=1, extraction=parsed)], 1).status, 'needs_review')

    def test_unknown_section_does_not_hide_other_schema_errors(self):
        for defect in ['missing_metadata', 'bad_cell', 'missing_section_key']:
            payload = qs()[0].extraction.model_dump()
            payload['sections'].append({'key': 'unknown_section', 'listed_row_count': 0, 'rows': []})
            if defect == 'missing_metadata':
                del payload['complete']
            elif defect == 'bad_cell':
                payload['sections'][0]['rows'][0]['cells']['technical']['state'] = 'invalid'
            else:
                del payload['sections'][0]['key']
            with self.subTest(defect=defect), self.assertRaises(ExtractionError):
                parse_page_json(json.dumps(payload))

    def test_mp_85_missing_states_preserve_text_and_show_partial_report(self):
        payload = mp_missing_states()
        parsed = parse_page_json(json.dumps(payload))
        self.assertFalse(parsed.complete)
        self.assertEqual(len(parsed.uncertainties), 1)
        self.assertIn('85 cell(s)', parsed.uncertainties[0])
        self.assertIn('section 3', parsed.uncertainties[0])
        for raw, row in zip(payload['sections'][2]['rows'], parsed.sections[2].rows):
            for key, cell in row.cells.items():
                self.assertEqual(cell.state, 'uncertain')
                self.assertEqual(cell.text, raw['cells'][key]['text'])
                self.assertEqual(cell.shaded, raw['cells'][key]['shaded'])
        report = review([ParsedPage(page=1, extraction=parsed)], 1)
        self.assertEqual(report.status, 'needs_review')
        self.assertTrue(report.passed_checks)
        self.assertFalse(any(check.rule.startswith('mp_production.') for check in report.passed_checks))
        self.assertEqual(sum(issue.rule == 'extraction.uncertain' for issue in report.issues), 34)

    def test_missing_state_recovery_works_for_each_form(self):
        from test_discard import discard
        for page in [mp()[0], qs()[0], lot()[0], discard()]:
            payload = page.extraction.model_dump()
            cell = next(iter(payload['sections'][0]['rows'][0]['cells'].values()))
            del cell['state']
            with self.subTest(form=payload['form_type']):
                parsed = parse_page_json(json.dumps(payload))
                restored = next(iter(parsed.sections[0].rows[0].cells.values()))
                self.assertEqual(restored.state, 'uncertain')
                self.assertEqual(restored.model_dump(exclude={'state'}), cell)
                self.assertFalse(parsed.complete)

    def test_missing_states_and_unknown_sections_recover_together(self):
        payload = mp_missing_states()
        payload['sections'].insert(0, {'key': 'unknown_header', 'listed_row_count': 0, 'rows': []})
        payload['uncertainties'] = ['Existing warning']
        parsed = parse_page_json(json.dumps(payload))
        self.assertEqual(len(parsed.sections), 3)
        self.assertEqual(parsed.uncertainties[0], 'Existing warning')
        self.assertIn('Skipped extracted section 1', parsed.uncertainties[1])
        self.assertIn('section 4', parsed.uncertainties[2])
        self.assertEqual(parsed.sections[2].rows[0].cells['produced'].state, 'uncertain')

    def test_missing_states_do_not_hide_unrecoverable_errors(self):
        for defect in ['text', 'state', 'complete', 'uncertainties']:
            payload = mp_missing_states()
            if defect == 'text':
                del payload['sections'][2]['rows'][0]['cells']['produced']['text']
            elif defect == 'state':
                payload['sections'][2]['rows'][0]['cells']['produced']['state'] = None
            else:
                del payload[defect]
            with self.subTest(defect=defect), self.assertRaises(ExtractionError):
                parse_page_json(json.dumps(payload))
