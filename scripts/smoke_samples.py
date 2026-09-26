"""Live LlamaParse smoke test against supplied synthetic examples (uses API credits)."""
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import fitz
from llama_extraction import run_extraction

root = Path('/workspace')
def check(path):
    with fitz.open(path) as doc:
        count = len(doc)
    try:
        report = run_extraction(path, count)
        target = root / '.review-dev' / (path.stem + '.json')
        target.parent.mkdir(exist_ok=True)
        target.write_text(report.model_dump_json(indent=2), encoding='utf-8')
        print(json.dumps({'file': path.name, 'form': report.form_type, 'status': report.status,
                          'checks': report.checks_run, 'issues': [i.model_dump() for i in report.issues]}), flush=True)
    except Exception as exc:
        print(json.dumps({'file': path.name, 'error_type': type(exc).__name__,
                          'error': str(exc) if type(exc).__name__ == 'ExtractionError' else 'Provider request failed'}), flush=True)
        raise

if __name__ == '__main__':
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(check, sorted((root / 'samples').iterdir())))
