import unittest

from reviewer.rules import review
from reviewer.models import Cell
from test_rules import cell, lot, mp, qs, signature
from test_discard import discard


class PassedCheckTests(unittest.TestCase):
    def test_all_supported_forms_include_values_and_page_locations(self):
        for pages in [qs(), mp(), lot(), [discard()]]:
            with self.subTest(form=pages[0].extraction.form_type):
                report = review(pages, len(pages))
                self.assertEqual(report.status, 'passed')
                self.assertTrue(report.passed_checks)
                for check in report.passed_checks:
                    self.assertIn(check.page, [p.page for p in pages])
                    self.assertTrue(check.message)
                    self.assertTrue(check.section)
                    self.assertIsInstance(check.observed, str)

    def test_invalid_review_entries_do_not_appear_as_passed(self):
        for entry in [cell(''), cell('?'), Cell(text='AB 04/23/26', state='uncertain'),
                      signature('AB 02/30/26', date='02/30/26'),
                      signature('AB', date=None), signature('04/23/26', initials=None),
                      signature('AB 04-23-26', date='04/23/26'), cell('NA')]:
            with self.subTest(entry=entry.text):
                pages = qs()
                pages[0].extraction.sections[0].rows[0].cells['technical'] = entry
                report = review(pages, 1)
                self.assertTrue(report.issues)
                self.assertFalse(any(c.row == 'Item 1: 1' and c.field == 'Technical'
                                     for c in report.passed_checks))
                self.assertTrue(any(c.field == 'Quality' for c in report.passed_checks))

    def test_na_and_conditional_inc_explain_why_the_rule_passed(self):
        pages = qs()
        pages[0].extraction.sections[0].rows[0].cells['technical'] = cell('n/a')
        report = review(pages, 1)
        self.assertTrue(any(c.rule == 'qs_reviews.na' and c.observed == 'n/a' for c in report.passed_checks))
        self.assertTrue(any(c.rule == 'qs_inc.not_required' for c in report.passed_checks))
        inc_row = pages[0].extraction.sections[1].rows[0]
        inc_row.cells['inc'] = cell('CT-001')
        self.assertFalse(any(c.rule.startswith('qs_inc.') for c in review(pages, 1).passed_checks))
        inc_row.cells['status'] = cell('Closed')
        check = next(c for c in review(pages, 1).passed_checks if c.rule == 'qs_inc.status')
        self.assertIn('CT-001', check.observed)
        self.assertIn('Closed', check.observed)

    def test_shaded_uncertain_and_blank_production_cells_are_not_passed(self):
        for entry in [cell('', shaded=True), cell('2', shaded=None), cell('')]:
            pages = mp()
            pages[0].extraction.sections[2].rows[0].cells['packaged'] = entry
            report = review(pages, 1)
            self.assertFalse(any(c.field == '# Packaged' for c in report.passed_checks))
            self.assertTrue(any(c.field == '# Produced' and c.observed == '0' for c in report.passed_checks))

    def test_discard_passes_follow_each_pages_own_status(self):
        pages = [discard('released_packaged', 'ID-01', 1), discard('unprocessed', 'ID-02', 2)]
        report = review(pages, 2)
        matches = [c for c in report.passed_checks if c.rule == 'discard_status.graft_match']
        self.assertEqual([c.page for c in matches], [1])
        self.assertIn('ID-01', matches[0].observed)
        self.assertTrue(any(i.page == 2 for i in report.issues))


if __name__ == '__main__':
    unittest.main()
