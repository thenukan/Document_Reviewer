import os
import time
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
        image=self.client.get(f'/api/reviews/{id}/pages/1')
        self.assertEqual(image.headers['content-type'],'image/png')
        self.assertTrue(image.content.startswith(b'\x89PNG'))
        self.assertEqual(self.client.get(f'/api/reviews/{id}/pages/2').status_code,404)
        report=self.client.get(f'/api/reviews/{id}/report')
        self.assertEqual(report.status_code,200);self.assertIn('attachment',report.headers['content-disposition'])
    def test_provider_failure_is_not_a_pass_or_secret_leak(self):
        with patch.object(module,'run_extraction',side_effect=RuntimeError('secret-api-key')):
            job=self.wait(self.send(pdf_bytes()).json()['id'])
        self.assertEqual(job['status'],'failed');self.assertNotIn('secret-api-key',str(job))
    def test_invalid_ids(self):
        self.assertEqual(self.client.get('/api/reviews/missing').status_code,404)


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
