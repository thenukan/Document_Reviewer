import copy
import unittest
from reviewer.models import Cell, Row, Section, PageExtraction, ParsedPage
from reviewer.rules import MP_HEADER, review
from llama_parse import parse_page_json, ExtractionError


def cell(text='AB 04/23/26', **kw):
    return Cell(text=text, state='filled' if text else 'blank', **kw)


def signature(text='AB 04/23/26', initials='AB', date='04/23/26'):
    return cell(text, initials=initials, date=date)


def section(key, rows):
    return Section(key=key, listed_row_count=len(rows), rows=rows)


def row(key, **cells):
    return Row(key=key, label=key, cells=cells)


def page(form, sections, number=1):
    return ParsedPage(page=number, extraction=PageExtraction(
        form_type=form, identification_text=form if form!='Lot Logs' else 'MS Processing & Packaging Lot Log MP-F-021',
        complete=True, uncertainties=[], sections=sections))


def qs():
    return [page('QS-F-049', [section('qs_reviews', [row(str(i), technical=signature(), quality=signature()) for i in range(1,11)]),
                                    section('qs_inc', [row('10', inc=cell(''), status=cell(''))])])]


def mp():
    return [page('MP-F-023', [section('mp_header', [row(k, value=signature()) for k in sorted(MP_HEADER)]),
                            section('mp_operations', [row('manager', value=signature())]),
                            section('mp_production', [row('Tendon', produced=cell('0'), packaged=cell('2'))])])]


def lot():
    return [page('Lot Logs', [section('lot_items',[row('Gloves',lot_number=cell('N/A'),expiration_date=cell('N/A'),manufacturer=cell('N/A'))]),
                             section('lot_regenmed',[row('Labels',lot=cell('IR-25-001'),quantity=cell('0'))])]),
            page('Lot Logs', [section('lot_sterilization',[row('Tray',load_number=cell('3'),sterilization_date=cell('15 APR 2026'))]),
                             section('lot_packaging',[row('Bag',lot=cell('IR-25-001'),quantity=cell('0'))])],2)]


class RuleTests(unittest.TestCase):
    def test_valid_forms(self):
        for factory in [qs,mp,lot]:
            with self.subTest(form=factory.__name__):
                pages=factory();self.assertEqual(review(pages,len(pages)).status,'passed')

    def test_qs_missing_initials_and_date(self):
        p=qs();p[0].extraction.sections[0].rows[0].cells['technical']=signature('04/23/26',None,'04/23/26')
        p[0].extraction.sections[0].rows[1].cells['quality']=signature('AB','AB',None)
        issues=review(p,1).issues
        self.assertEqual({i.rule for i in issues},{'qs_reviews.initials','qs_reviews.date'})

    def test_qs_original_date_format_and_calendar(self):
        for date in ['4/23/26','04-23-26','04.23.26','04/23/2026','02/30/26','13/01/26']:
            with self.subTest(date=date):
                p=qs();p[0].extraction.sections[0].rows[0].cells['technical']=signature('AB '+date,date=date)
                self.assertIn('qs_reviews.date_format',[i.rule for i in review(p,1).issues])

    def test_qs_na_is_exempt(self):
        p=qs();p[0].extraction.sections[0].rows[0].cells['technical']=cell('n/a')
        self.assertEqual(review(p,1).status,'passed')

    def test_inc_requires_adjacent_status(self):
        p=qs();p[0].extraction.sections[1].rows[0].cells['inc']=cell('CT-001')
        self.assertEqual(review(p,1).issues[0].field,'Status')
        p[0].extraction.sections[1].rows[0].cells['status']=cell('Closed')
        self.assertEqual(review(p,1).status,'passed')

    def test_mp_each_white_cell_required_zero_counts(self):
        p=mp();r=p[0].extraction.sections[2].rows[0];r.cells['packaged']=cell('')
        self.assertEqual(review(p,1).issues[0].field,'# Packaged')
        r.cells['packaged'].shaded=True
        self.assertEqual(review(p,1).status,'passed')
        r.cells['packaged'].shaded=None
        self.assertEqual(review(p,1).status,'needs_review')

    def test_mp_header_and_manager(self):
        p=mp();p[0].extraction.sections[0].rows[0].cells['value']=cell('')
        p[0].extraction.sections[1].rows[0].cells['value']=signature('AB','AB',None)
        issues=review(p,1).issues
        self.assertEqual(len(issues),2)
        self.assertIn('mp_operations.date',[i.rule for i in issues])

    def test_lot_all_required_cells(self):
        cases=[(0,0,'lot_number'),(0,0,'expiration_date'),(0,0,'manufacturer'),(0,1,'lot'),(0,1,'quantity'),
               (1,0,'load_number'),(1,0,'sterilization_date'),(1,1,'lot'),(1,1,'quantity')]
        for pi,si,field in cases:
            with self.subTest(field=field):
                p=lot();p[pi].extraction.sections[si].rows[0].cells[field]=cell('')
                report=review(p,2);self.assertEqual(report.status,'issues_found');self.assertEqual(len(report.issues),1)

    def test_missing_lot_page_blocks_pass(self):
        self.assertEqual(review(lot()[:1],1).status,'needs_review')

    def test_missing_section_or_row_never_passes(self):
        p=qs();p[0].extraction.sections.pop();self.assertEqual(review(p,1).status,'needs_review')
        p=qs();p[0].extraction.sections[0].rows.pop();self.assertEqual(review(p,1).status,'needs_review')

    def test_unknown_mixed_and_incomplete_never_pass(self):
        p=qs();p[0].extraction.identification_text='unrelated document';self.assertEqual(review(p,1).status,'unsupported')
        p=qs();p[0].extraction.complete=False;self.assertEqual(review(p,1).status,'needs_review')
        p=qs()+mp();self.assertEqual(review(p,2).status,'unsupported')

    def test_uncertain_handwriting_never_passes(self):
        p=qs();p[0].extraction.sections[0].rows[0].cells['quality'].state='uncertain'
        self.assertEqual(review(p,1).status,'needs_review')

    def test_normalized_date_cannot_hide_original_format(self):
        p=qs();p[0].extraction.sections[0].rows[0].cells['technical']=signature('AB 04-23-26',date='04/23/26')
        self.assertEqual(review(p,1).status,'needs_review')

    def test_ambiguous_na_needs_verification(self):
        p=qs();p[0].extraction.sections[0].rows[0].cells['quality']=cell('NA')
        self.assertEqual(review(p,1).issues[0].rule,'qs_reviews.na_ambiguous')

    def test_unused_requires_explicit_na(self):
        p=lot();p[0].extraction.sections[0].rows[0].cells['manufacturer']=cell('unused')
        self.assertEqual(review(p,2).status,'issues_found')
        p[0].extraction.sections[0].rows[0].cells['manufacturer']=cell('—')
        self.assertEqual(review(p,2).status,'needs_review')

    def test_parser_fenced_json_and_invalid_outputs(self):
        extracted=qs()[0].extraction
        self.assertEqual(parse_page_json('```json\n'+extracted.model_dump_json()+'\n```'),extracted)
        for text in ['{}','plain text','{"form_type":"QS-F-049"}']:
            with self.assertRaises(ExtractionError):parse_page_json(text)


if __name__=='__main__':unittest.main()

