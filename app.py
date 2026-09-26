"""Single-PDF RegenMed pre-review application."""
import json
import logging
import os
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import fitz
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from llama_extraction import run_extraction
from llama_parse import ExtractionError

load_dotenv()
ROOT = Path(__file__).resolve().parent
MAX_BYTES = int(os.getenv('MAX_UPLOAD_MB', '20')) * 1024 * 1024
MAX_PAGES = int(os.getenv('MAX_PAGES', '10'))
TTL = max(60, int(os.getenv('REVIEW_TTL_SECONDS', '3600')))
MAX_JOBS = 24
log = logging.getLogger('regenmed')


def configured():
    return bool(os.getenv('LLAMA_CLOUD_API_KEY') or os.getenv('LLAMA_PARSE_API_KEY'))


class ReviewStore:
    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix='regenmed-'))
        self.jobs = {}
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='review')
        self.stop = threading.Event()
        self.cleaner = threading.Thread(target=self._sweep, daemon=True)
        self.cleaner.start()

    def _sweep(self):
        while not self.stop.wait(30):
            with self.lock:
                for job_id, job in list(self.jobs.items()):
                    if job['status'] in {'completed', 'failed'} and time.time() - job['finished'] > TTL:
                        shutil.rmtree(self.root / job_id, ignore_errors=True)
                        del self.jobs[job_id]

    def get(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise HTTPException(404, 'Review not found or expired. Upload the PDF again.')
            return dict(job)

    def update(self, job_id, **values):
        with self.lock:
            self.jobs[job_id].update(values)

    def close(self):
        self.stop.set()
        self.cleaner.join(timeout=2)
        self.pool.shutdown(wait=True, cancel_futures=True)
        shutil.rmtree(self.root, ignore_errors=True)


@asynccontextmanager
async def lifespan(app):
    app.state.reviews = ReviewStore()
    yield
    app.state.reviews.close()


app = FastAPI(title='RegenMed Internal Document Reviewer', version='1.0.0', lifespan=lifespan)
app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')


@app.middleware('http')
async def response_headers(request, call_next):
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    return response


@app.get('/', include_in_schema=False)
def index():
    return FileResponse(ROOT / 'static' / 'index.html')


@app.get('/healthz')
def health():
    return {'status': 'ok', 'parser_configured': configured(), 'max_upload_mb': MAX_BYTES // 1024 // 1024}


def process_review(store, job_id, pdf_path, page_count):
    store.update(job_id, status='processing', stage='Reading the form with LlamaParse')
    try:
        report = run_extraction(pdf_path, page_count)
        store.update(job_id, status='completed', stage='Review complete',
                     report=report.model_dump(mode='json'), finished=time.time())
    except Exception as exc:
        # Provider exceptions can contain credentials/URLs. Do not expose or log them.
        message = str(exc) if isinstance(exc, ExtractionError) else 'The document service could not complete this review. Try again shortly.'
        status = getattr(exc, 'status_code', None)
        if status in (401, 403):
            message = 'LlamaParse rejected the server API key. Check its permissions and configuration.'
        elif status == 429:
            message = 'LlamaParse is rate-limited or has insufficient credits. Retry after checking your account.'
        elif 'Timeout' in type(exc).__name__:
            message = 'LlamaParse timed out. Retry with a clearer or smaller PDF.'
        log.warning('Review %s failed (%s)', job_id, type(exc).__name__)
        store.update(job_id, status='failed', stage='Review could not complete', error=message, finished=time.time())


@app.post('/api/reviews', status_code=202)
def upload_review(file: UploadFile = File(...)):
    if not configured():
        raise HTTPException(503, 'Set LLAMA_PARSE_API_KEY or LLAMA_CLOUD_API_KEY on the server to enable reviews.')
    if not file.filename or Path(file.filename).suffix.lower() != '.pdf':
        raise HTTPException(415, 'Upload one PDF file.')
    store = app.state.reviews
    job_id = uuid4().hex
    with store.lock:
        if len(store.jobs) >= MAX_JOBS:
            raise HTTPException(503, 'The review queue is full. Try again after existing reviews expire.')
        directory = store.root / job_id
        directory.mkdir()
        store.jobs[job_id] = {'id': job_id, 'filename': Path(file.filename.replace('\\', '/')).name,
                              'status': 'uploading', 'stage': 'Checking PDF', 'created': time.time()}
    pdf_path = directory / 'source.pdf'
    try:
        size = 0
        with pdf_path.open('wb') as target:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise HTTPException(413, f'PDF exceeds the {MAX_BYTES // 1024 // 1024} MB limit.')
                target.write(chunk)
        with pdf_path.open('rb') as source:
            if b'%PDF-' not in source.read(1024):
                raise HTTPException(415, 'The uploaded file is not a valid PDF.')
        try:
            with fitz.open(pdf_path) as doc:
                if doc.needs_pass:
                    raise HTTPException(422, 'Password-protected PDFs are not supported.')
                count = doc.page_count
                if count < 1 or count > MAX_PAGES:
                    raise HTTPException(422, f'Upload a PDF containing 1 to {MAX_PAGES} pages.')
        except (fitz.FileDataError, RuntimeError) as exc:
            raise HTTPException(422, 'The PDF is damaged or cannot be read.') from exc
        store.update(job_id, status='queued', stage='Waiting for LlamaParse', page_count=count)
        store.pool.submit(process_review, store, job_id, pdf_path, count)
        return {'id': job_id, 'status': 'queued', 'status_url': f'/api/reviews/{job_id}'}
    except Exception:
        with store.lock:
            store.jobs.pop(job_id, None)
        shutil.rmtree(directory, ignore_errors=True)
        raise
    finally:
        file.file.close()


@app.get('/api/reviews/{job_id}')
def review_status(job_id: str):
    return app.state.reviews.get(job_id)


@app.get('/api/reviews/{job_id}/report')
def download_report(job_id: str):
    job = app.state.reviews.get(job_id)
    if job['status'] != 'completed':
        raise HTTPException(409, 'The review is not complete.')
    return Response(json.dumps(job['report'], indent=2, ensure_ascii=False), media_type='application/json',
                    headers={'Content-Disposition': 'attachment; filename="regenmed-review.json"'})


@app.get('/api/reviews/{job_id}/pages/{page_number}')
def page_preview(job_id: str, page_number: int):
    store = app.state.reviews
    job = store.get(job_id)
    if not 1 <= page_number <= job.get('page_count', 0):
        raise HTTPException(404, 'Page not found.')
    with store.lock:
        path = store.root / job_id / 'source.pdf'
        if not path.exists():
            raise HTTPException(404, 'Review has expired.')
        with fitz.open(path) as doc:
            page = doc[page_number - 1]
            zoom = min(2, 1600 / max(page.rect.width, page.rect.height))
            png = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False).tobytes('png')
    return Response(png, media_type='image/png')

