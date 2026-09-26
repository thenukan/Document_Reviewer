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
