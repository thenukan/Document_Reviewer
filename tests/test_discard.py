import unittest

from reviewer.discard import BOTTOM_FIELDS, STATUS_FIELDS
from reviewer.rules import identify, review
from test_rules import cell, page, row, section, signature


def box(checked=False, text=None):
    return cell(('X' if checked else '') if text is None else text, checked=checked)


def discard(status='in_processing', graft='N/A', number=1):
    return page('Discard Form', [
        section('discard_header', [row('donor_number', value=cell('22043')),
                                  row('authorized_by_date', value=signature('KS 01-20-25','KS','01-20-25')),
                                  row('reason_for_discard', value=cell('Production goals'))]),
        section('discard_status', [row(key, value=box(key==status)) for key in STATUS_FIELDS]),
        section('discard_tissues', [row('1 — Tendon', graft_id=cell(graft), confirmation_x=box(True))]),
        section('discard_bottom', [row(key, value=cell('N/A' if key.startswith('distribution') else 'Recorded')) for key in BOTTOM_FIELDS]),
    ], number)


class DiscardTests(unittest.TestCase):
    def test_detect_by_title_and_form_code(self):
        for text in ['Tissue Discard Form', 'Discard Form', 'MP-F-018.005', 'MP F 018']:
            self.assertEqual(identify(text), 'Discard Form')
        self.assertEqual(identify('MP-F-023 / MP-F-018'), 'Unknown')
        self.assertEqual(identify('Reason for Discard'), 'Unknown')

    def test_all_four_valid_statuses(self):
        for status in STATUS_FIELDS:
            graft='FRZ-001' if 'packaged' in status else 'N/A'
            with self.subTest(status=status):self.assertEqual(review([discard(status,graft)],1).status,'passed')

    def test_header_blanks_and_authorization_components(self):
        for index in range(3):
            p=discard();p.extraction.sections[0].rows[index].cells['value']=cell('')
            self.assertIn('discard_header.required',[i.rule for i in review([p],1).issues])
        for initials,date in [(None,'01-20-25'),('KS',None),(None,None)]:
            p=discard();p.extraction.sections[0].rows[1].cells['value']=signature('KS 01-20-25',initials,date)
            self.assertEqual(review([p],1).status,'issues_found')

    def test_zero_multiple_and_uncertain_statuses(self):
        p=discard(status=None);self.assertIn('discard_status.required',[i.rule for i in review([p],1).issues])
        p=discard();p.extraction.sections[1].rows[0].cells['value']=box(True)
        self.assertIn('discard_status.multiple',[i.rule for i in review([p],1).issues])
        p=discard();p.extraction.sections[1].rows[0].cells['value']=cell('?',checked=None)
        self.assertEqual(review([p],1).status,'needs_review')

    def test_graft_status_matrix(self):
        for status in STATUS_FIELDS:
            for graft in ['N/A','ID-UNSEEN-27']:
                with self.subTest(status=status,graft=graft):
                    mismatch=(graft=='N/A')==('packaged' in status)
                    report=review([discard(status,graft)],1)
                    self.assertEqual(any(i.rule=='discard_status.graft_mismatch' for i in report.issues),mismatch)

    def test_mixed_graft_ids_and_na_cannot_pass(self):
        p=discard('released_packaged','ID-01')
        s=p.extraction.sections[2];s.rows.append(row('2 — Bone',graft_id=cell('N/A'),confirmation_x=box(True)));s.listed_row_count=2
        self.assertEqual(review([p],1).status,'issues_found')

    def test_missing_or_struck_through_graft_needs_verification(self):
        for graft in ['', '—', 'NA', '[illegible]']:
            with self.subTest(graft=graft):self.assertEqual(review([discard(graft=graft)],1).status,'needs_review')
        p=discard();del p.extraction.sections[2].rows[0].cells['graft_id']
        self.assertEqual(review([p],1).status,'needs_review')

    def test_x_box_required_for_every_listed_row(self):
        p=discard();s=p.extraction.sections[2]
        s.rows.append(row('2 — Bone',graft_id=cell('N/A'),confirmation_x=box(False)));s.listed_row_count=2
        report=review([p],1)
        self.assertEqual(report.issues[0].rule,'discard_tissues.confirmation')
        self.assertEqual(report.issues[0].row,'2 — Bone')
        s.rows[1].cells['confirmation_x']=cell('N/A',checked=True)
        self.assertEqual(review([p],1).status,'needs_review')
        del s.rows[1].cells['confirmation_x']
        self.assertEqual(review([p],1).status,'needs_review')

    def test_contradictory_checkbox_evidence_blocks_pass(self):
        for checked,text in [(True,''),(False,'X'),(True,'?')]:
            p=discard();p.extraction.sections[2].rows[0].cells['confirmation_x']=box(checked,text)
            self.assertEqual(review([p],1).status,'needs_review')

    def test_all_bottom_fields_required_even_inactive_pair(self):
        for index,key in enumerate(BOTTOM_FIELDS):
            with self.subTest(field=key):
                p=discard();p.extraction.sections[3].rows[index].cells['value']=cell('')
                self.assertIn('discard_bottom.required',[i.rule for i in review([p],1).issues])
        p=discard();s=p.extraction.sections[3];s.rows.append(row('extra_field',value=cell('')));s.listed_row_count+=1
        self.assertEqual(review([p],1).status,'issues_found')

    def test_sections_and_known_fields_cannot_be_omitted(self):
        for si in range(4):
            p=discard();p.extraction.sections.pop(si)
            self.assertEqual(review([p],1).status,'needs_review')
        p=discard();s=p.extraction.sections[3];s.rows.pop();s.listed_row_count-=1
        self.assertEqual(review([p],1).status,'needs_review')

    def test_repeated_form_pages_use_their_own_status(self):
        pages=[discard('released_packaged','ID-01',1),discard('unprocessed','N/A',2),discard(number=3)]
        self.assertEqual(review(pages,3).status,'passed')
        pages[1].extraction.sections[2].rows[0].cells['graft_id']=cell('ID-02')
        issues=review(pages,3).issues
        self.assertEqual(len(issues),1);self.assertEqual(issues[0].page,2)
        pages[2].extraction.sections.pop(3)
        self.assertTrue(any(i.rule=='extraction.missing_section' and i.page==3 for i in review(pages,3).issues))

    def test_no_qs_date_format_rule_on_discard(self):
        self.assertEqual(review([discard()],1).status,'passed')


if __name__=='__main__':unittest.main()
