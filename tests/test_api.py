import os
import time
import json
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path

import fitz
from fastapi.testclient import TestClient
import app as module
from llama_parse import ExtractionError, run_parse
from test_rules import qs
from reviewer.rules import review


def pdf_bytes(pages=1, encrypted=False):
    with fitz.open() as doc:
        for _ in range(pages):doc.new_page()
        if encrypted:return doc.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256,owner_pw='owner',user_pw='secret')
        return doc.tobytes()


class APITests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,{'LLAMA_PARSE_API_KEY':'test-only'});self.env.start()
        self.client=TestClient(module.app);self.client.__enter__()
    def tearDown(self):
        self.client.__exit__(None,None,None);self.env.stop()
    def send(self,data,name='form.pdf'):
        return self.client.post('/api/reviews',files={'file':(name,data,'application/pdf')})
    def test_home_health_and_validation(self):
        self.assertEqual(self.client.get('/').status_code,200)
        self.assertTrue(self.client.get('/healthz').json()['parser_configured'])
        self.assertEqual(self.send(b'garbage').status_code,415)
        self.assertEqual(self.send(b'%PDF-broken').status_code,422)
        self.assertEqual(self.send(pdf_bytes(),'form.txt').status_code,415)
        self.assertEqual(self.send(pdf_bytes(encrypted=True)).status_code,422)
        with patch.object(module,'MAX_BYTES',10):self.assertEqual(self.send(pdf_bytes()).status_code,413)
        with patch.object(module,'MAX_PAGES',1):self.assertEqual(self.send(pdf_bytes(2)).status_code,422)
    def test_missing_configuration(self):
        with patch.dict(os.environ,{},clear=True):self.assertEqual(self.send(pdf_bytes()).status_code,503)
    def wait(self,id):
        for _ in range(100):
            data=self.client.get('/api/reviews/'+id).json()
            if data['status'] in ['completed','failed']:return data
            time.sleep(.01)
        self.fail('Job did not finish')
    def test_full_upload_poll_report_and_preview(self):
        with patch.object(module,'run_extraction',return_value=review(qs(),1)):
            response=self.send(pdf_bytes());self.assertEqual(response.status_code,202)
            id=response.json()['id'];job=self.wait(id)
        self.assertEqual(job['report']['status'],'passed')
        self.assertTrue(job['report']['passed_checks'])
        self.assertEqual(job['report']['passed_checks'][0]['page'],1)
        image=self.client.get(f'/api/reviews/{id}/pages/1')
        self.assertEqual(image.headers['content-type'],'image/png')
        self.assertTrue(image.content.startswith(b'\x89PNG'))
        self.assertEqual(self.client.get(f'/api/reviews/{id}/pages/2').status_code,404)
        report=self.client.get(f'/api/reviews/{id}/report')
        self.assertEqual(report.status_code,200);self.assertIn('attachment',report.headers['content-disposition'])
        self.assertEqual(report.json()['passed_checks'],job['report']['passed_checks'])
    def test_provider_failure_is_not_a_pass_or_secret_leak(self):
        with patch.object(module,'run_extraction',side_effect=RuntimeError('secret-api-key')):
            job=self.wait(self.send(pdf_bytes()).json()['id'])
        self.assertEqual(job['status'],'failed');self.assertNotIn('secret-api-key',str(job))
    def test_extraction_failure_logs_safe_diagnostic_and_prevents_report(self):
        error=ExtractionError('PDF page 2: transcription does not match the required form structure.',
                              diagnostic='page 2: schema validation: form_type: missing')
        with patch.object(module,'run_extraction',side_effect=error),self.assertLogs('regenmed','WARNING') as logs:
            job=self.wait(self.send(pdf_bytes(2)).json()['id'])
        self.assertEqual(job['status'],'failed')
        self.assertIn('PDF page 2',job['error'])
        self.assertIn(error.diagnostic,logs.output[0])
        self.assertEqual(self.client.get('/api/reviews/'+job['id']+'/report').status_code,409)
    def test_unknown_section_returns_partial_report_and_other_pages(self):
        extracted=qs()[0].extraction.model_dump()
        extracted['sections'].insert(0,{'key':'unknown_header','listed_row_count':0,'rows':[]})
        response={'job':{'status':'COMPLETED'},'markdown':{'pages':[
            {'page_number':1,'markdown':json.dumps(extracted)},
            {'page_number':2,'markdown':qs()[0].extraction.model_dump_json()},
        ]}}
        with patch('llama_parse.LlamaCloud') as cls:
            cls.return_value.__enter__.return_value.parsing.parse.return_value.model_dump.return_value=response
            job=self.wait(self.send(pdf_bytes(2)).json()['id'])
        self.assertEqual(job['status'],'completed')
        report=job['report']
        self.assertEqual(report['status'],'needs_review')
        self.assertTrue(report['passed_checks'])
        self.assertEqual(len(report['extracted_pages']),2)
        self.assertFalse(report['extracted_pages'][0]['extraction']['complete'])
        self.assertTrue(report['extracted_pages'][1]['extraction']['complete'])
        warnings=[issue for issue in report['issues'] if issue['rule']=='extraction.incomplete']
        self.assertEqual(len(warnings),1)
        self.assertEqual(warnings[0]['page'],1)
        self.assertIn('Skipped extracted section 1',warnings[0]['message'])
        self.assertEqual(self.client.get('/api/reviews/'+job['id']+'/report').json(),report)
        self.assertEqual(self.client.get('/api/reviews/'+job['id']+'/pages/1').status_code,200)
    def test_discard_report_keeps_page_specific_findings(self):
        from test_discard import discard
        pages=[discard('released_packaged','ID-01',1),discard('unprocessed','ID-02',2),discard(number=3)]
        expected=review(pages,3)
        with patch.object(module,'run_extraction',return_value=expected):
            response=self.send(pdf_bytes(3),'bonus-round.pdf')
            self.assertEqual(response.status_code,202)
            job=self.wait(response.json()['id'])
        report=job['report']
        self.assertEqual(report['form_type'],'Discard Form')
        self.assertEqual(report['status'],'issues_found')
        self.assertEqual(report['issues'][0]['page'],2)
        self.assertEqual(report['issues'][0]['rule'],'discard_status.graft_mismatch')
        downloaded=self.client.get('/api/reviews/'+job['id']+'/report').json()
        self.assertEqual(downloaded,report)
        self.assertEqual(self.client.get('/api/reviews/'+job['id']+'/pages/3').status_code,200)

    def test_non_list_uncertainties_and_unknown_sections_return_partial_report(self):
        extracted=qs()[0].extraction.model_dump()
        valid_sections=extracted['sections'][:]
        extracted['uncertainties']={'note':'Verify the handwritten date'}
        extracted['sections'][:0]=[
            {'key':'unknown_header','listed_row_count':0,'rows':[]},
            {'key':'unknown_footer','listed_row_count':0,'rows':[]},
        ]
        response={'job':{'status':'COMPLETED'},'markdown':{'pages':[
            {'page_number':1,'markdown':json.dumps(extracted)},
            {'page_number':2,'markdown':qs()[0].extraction.model_dump_json()},
        ]}}
        with patch('llama_parse.LlamaCloud') as cls:
            cls.return_value.__enter__.return_value.parsing.parse.return_value.model_dump.return_value=response
            job=self.wait(self.send(pdf_bytes(2)).json()['id'])
        self.assertEqual(job['status'],'completed')
        report=job['report']
        self.assertEqual(report['status'],'needs_review')
        self.assertTrue(report['passed_checks'])
        self.assertEqual(report['extracted_pages'][0]['extraction']['sections'],valid_sections)
        self.assertFalse(report['extracted_pages'][0]['extraction']['complete'])
        self.assertTrue(report['extracted_pages'][1]['extraction']['complete'])
        warnings=[issue for issue in report['issues'] if issue['rule']=='extraction.incomplete']
        self.assertEqual(len(warnings),1)
        self.assertEqual(warnings[0]['page'],1)
        self.assertIn('unexpected format',warnings[0]['message'])
        self.assertIn('Verify the handwritten date',warnings[0]['message'])
        self.assertIn('Skipped extracted section 1',warnings[0]['message'])
        self.assertIn('Skipped extracted section 2',warnings[0]['message'])
        self.assertEqual(self.client.get('/api/reviews/'+job['id']+'/report').json(),report)
        self.assertEqual(self.client.get('/api/reviews/'+job['id']+'/pages/1').status_code,200)

    def test_mp_missing_states_returns_downloadable_partial_report(self):
        from test_parser_errors import mp_missing_states
        payload={'job':{'status':'COMPLETED'},'markdown':{'pages':[
            {'page_number':1,'markdown':json.dumps(mp_missing_states())}
        ]}}
        with patch('llama_parse.LlamaCloud') as cls:
            cls.return_value.__enter__.return_value.parsing.parse.return_value.model_dump.return_value=payload
            job=self.wait(self.send(pdf_bytes()).json()['id'])
        self.assertEqual(job['status'],'completed')
        report=job['report']
        self.assertEqual(report['form_type'],'MP-F-023')
        self.assertEqual(report['status'],'needs_review')
        self.assertTrue(report['passed_checks'])
        self.assertIn('85 cell(s)',report['issues'][0]['message'])
        cell=report['extracted_pages'][0]['extraction']['sections'][2]['rows'][0]['cells']['produced']
        self.assertEqual(cell['text'],'0')
        self.assertEqual(cell['state'],'uncertain')
        self.assertEqual(self.client.get('/api/reviews/'+job['id']+'/report').json(),report)
        self.assertEqual(self.client.get('/api/reviews/'+job['id']+'/pages/1').status_code,200)

    def test_missing_row_label_returns_downloadable_partial_report(self):
        extracted=qs()[0].extraction.model_dump()
        cells=extracted['sections'][1]['rows'][0]['cells']
        cells['inc'].update(text='CT-001',state='filled')
        cells['status'].update(text='Closed',state='filled')
        expected_sections=json.loads(json.dumps(extracted['sections']))
        expected_sections[1]['rows'][0]['label']='Row 1 (label unavailable)'
        del extracted['sections'][1]['rows'][0]['label']
        valid_page=qs()[0].extraction.model_dump()
        response={'job':{'status':'COMPLETED'},'markdown':{'pages':[
            {'page_number':1,'markdown':json.dumps(extracted)},
            {'page_number':2,'markdown':json.dumps(valid_page)},
        ]}}
        with patch('llama_parse.LlamaCloud') as cls:
            cls.return_value.__enter__.return_value.parsing.parse.return_value.model_dump.return_value=response
            job=self.wait(self.send(pdf_bytes(2)).json()['id'])
        self.assertEqual(job['status'],'completed')
        report=job['report']
        self.assertEqual(report['status'],'needs_review')
        self.assertTrue(report['passed_checks'])
        self.assertEqual(len(report['extracted_pages']),2)
        recovered=report['extracted_pages'][0]['extraction']
        self.assertFalse(recovered['complete'])
        self.assertEqual(recovered['sections'],expected_sections)
        self.assertEqual(report['extracted_pages'][1]['extraction'],valid_page)
        warnings=[issue for issue in report['issues'] if issue['rule']=='extraction.incomplete']
        self.assertEqual(len(warnings),1)
        self.assertEqual(warnings[0]['page'],1)
        message=warnings[0]['message'].lower()
        self.assertIn('section 2',message)
        self.assertIn('row 1',message)
        self.assertRegex(message,r'(missing|unavailable).*label|label.*(missing|unavailable)')
        download=self.client.get('/api/reviews/'+job['id']+'/report')
        self.assertEqual(download.status_code,200)
        self.assertIn('attachment',download.headers['content-disposition'])
        self.assertEqual(download.json(),report)
        preview=self.client.get('/api/reviews/'+job['id']+'/pages/1')
        self.assertEqual(preview.status_code,200)
        self.assertEqual(preview.headers['content-type'],'image/png')
        self.assertTrue(preview.content.startswith(b'\x89PNG'))

    def test_invalid_ids(self):
        self.assertEqual(self.client.get('/api/reviews/missing').status_code,404)

    def test_rules_failure_is_logged_and_not_blamed_on_provider(self):
        with patch('llama_extraction.run_parse',return_value=qs()),patch('llama_extraction.review',side_effect=KeyError('bug')),\
                self.assertLogs('regenmed','ERROR') as logs:
            job=self.wait(self.send(pdf_bytes()).json()['id'])
        self.assertEqual(job['status'],'failed');self.assertIn('Internal error',job['error'])
        self.assertIn('Traceback',logs.output[0])

    def test_bad_parser_config_rejects_upload_and_shows_in_health(self):
        with patch.dict(os.environ,{'LLAMA_PARSE_TIER':'fast','LLAMA_PARSE_TIMEOUT':'soon'}):
            errors=self.client.get('/healthz').json()['config_errors']
            self.assertEqual(len(errors),2)
            response=self.send(pdf_bytes())
        self.assertEqual(response.status_code,503);self.assertIn('LLAMA_PARSE_TIER',response.json()['detail'])
        self.assertEqual(self.client.get('/healthz').json()['config_errors'],[])

    def test_bad_integer_setting_falls_back_to_default(self):
        with patch.dict(os.environ,{'MAX_PAGES':'ten'}),patch.object(module,'CONFIG_WARNINGS',[]):
            self.assertEqual(module.env_int('MAX_PAGES',10),10)
            self.assertIn('MAX_PAGES',module.CONFIG_WARNINGS[0])

    def test_storage_failure_returns_clear_error(self):
        with patch.object(Path,'open',side_effect=OSError('No space left on device')):
            response=self.send(pdf_bytes())
        self.assertEqual(response.status_code,503);self.assertIn('could not store',response.json()['detail'])
        self.assertEqual(self.client.app.state.reviews.jobs,{})

    def test_preview_render_failure_returns_message(self):
        with patch.object(module,'run_extraction',return_value=review(qs(),1)):
            id=self.wait(self.send(pdf_bytes()).json()['id'])['id']
        with patch.object(module.fitz,'open',side_effect=RuntimeError('broken')),self.assertLogs('regenmed','ERROR'):
            response=self.client.get(f'/api/reviews/{id}/pages/1')
        self.assertEqual(response.status_code,500);self.assertEqual(response.json()['detail'],'The page preview could not be rendered.')


class AdapterTests(unittest.TestCase):
    def test_page_coverage_and_current_sdk_shape(self):
        model=qs()[0].extraction.model_dump_json()
        good={'job':{'status':'COMPLETED'},'markdown':{'pages':[{'page_number':1,'success':True,'markdown':model}]}}
        for payload,count,success in [(good,1,True),(good,2,False),({'job':{'status':'FAILED'}},1,False),
                ({'job':{'status':'COMPLETED'},'markdown':{'pages':[{'page_number':1,'success':False}]}},1,False)]:
            with self.subTest(payload=payload),patch('llama_parse.LlamaCloud') as cls,patch.dict(os.environ,{'LLAMA_PARSE_API_KEY':'test-only'}):
                client=cls.return_value.__enter__.return_value
                client.parsing.parse.return_value.model_dump.return_value=payload
                if success:self.assertEqual(len(run_parse(Path('unseen.pdf'),count)),1)
                else:
                    with self.assertRaises(ExtractionError):run_parse(Path('unseen.pdf'),count)


if __name__=='__main__':unittest.main()
