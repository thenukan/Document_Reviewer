# RegenMed Internal Document Reviewer

A Dockerized FastAPI web application for the RegenMed hackathon. Open the home page, upload one PDF, and review findings alongside the original scanned pages. The app identifies MP-F-023, QS-F-049, Lot Logs, and the bonus-round Discard Form (MP-F-018) from document content, with no filename matching or stored sample answers.

## Start with Docker

1. Install/start Docker Desktop (Linux containers).
2. If `.env` does not already exist, copy `.env.example` to `.env`.
3. Set `LLAMA_PARSE_API_KEY` to your LlamaCloud key. The existing key name is supported; `LLAMA_CLOUD_API_KEY` also works and takes precedence. No OpenAI, Azure, SQL Server, or notification service is required.
4. Run:

```sh
docker compose up --build -d
```

Open **http://localhost:8000**. API documentation is at `/docs`; health/configuration status is at `/healthz`.

```sh
docker compose logs -f reviewer
docker compose down
```

Your existing `.env` is preserved. Compose forwards only the reviewer settings, not the old service credentials. Credentials and sample PDFs are excluded from the image.

## How review works

1. Validate the uploaded PDF (default limits: 20 MB, 10 pages; encrypted/damaged files are rejected).
2. LlamaParse v2's agentic parser reads scanned handwriting and transcribes each page into a schema. It preserves raw dates, N/A, blank cells, shading, row identities, and initials/date components. The uploaded filename is not used for identification.
3. Pydantic validates the transcription. Python code identifies the form from extracted printed identifiers/titles and applies the challenge checks. The parser does not decide whether the form passes.
4. Findings identify the page, section, row, field, and observed value. Clicking a finding selects its page. Expand **View passed checks** to inspect successful rules and their supporting values, including when all checks pass; each entry links to its original page. Related checks (such as initials and date) are grouped, and exempt shaded cells are omitted. The UI also shows extracted values and offers a JSON report download including `passed_checks`.

The adapter uses the [official LlamaParse Python SDK](https://developers.llamaindex.ai/llamaparse/parse/getting_started/) and its custom parsing prompt. It requires a funded LlamaCloud account. No separate LLM provider is used.

| Form | Checks |
| --- | --- |
| MP-F-023 | Every top field from Donor # through Tissue Checked In By/Date; both initials and date for By/Date fields; Operations Manager initials/date; each white Produced/Packaged cell. |
| QS-F-049 | Every Technical and Quality row has initials plus a date, or explicit N/A; dates must be valid calendar dates in 2-digit MM/DD/YY order, written with `/`, `-` or `.` (used consistently); an entered INC # requires adjacent Status. |
| Lot Logs | Page 1 Item Lot Number, Exp. Date, Manufacturer; RegenMed Item Lot and Qty Used; page 2 Item Load # and Sterilization Date; Packaging Lot and Qty Used. |

| Discard Form (MP-F-018) | Donor #, Reason for Discard, authorization initials/date; one Tissue Status selected; status consistent with entered graft IDs or N/A; each listed tissue’s X box completed; every bottom field entered. |

Discard Forms are checked per page, including PDFs containing multiple completed forms. All four status boxes are transcribed explicitly; zero or multiple selections are flagged. Actual graft IDs require Unreleased Packaged Tissue or Released Packaged Tissue. Explicit N/A requires Unprocessed Tissue or In Processing Tissue; mixed actual IDs and N/A cannot silently pass. Unreadable, blank, or struck-through graft entries require manual verification rather than being assumed to be N/A. Blank spare tissue rows are ignored. All seven bottom fields in the reference layout, plus any additional extracted bottom fields, are checked; explicit N/A counts as entered. The bonus requirements do not impose the QS date-format rule on Discard Forms.

Interpretation of “Lot or Qty Used” and “Produced or Packaged”: a blank in **either applicable column** is flagged. Shaded MP production cells are exempt. Zero counts as entered. Entirely unlabeled spare rows are ignored; labeled rows with no entries are checked. N/A must be explicit, rather than guessed from a blank or strike-through. QS requires the slash form `N/A` (case-insensitive); ambiguous abbreviations still need staff verification.

There are no external product-code lookups, donor-system integrations, or extra release/temperature rules. The detailed challenge requirements define the checks.

Missing pages, missing sections, unreadable handwriting, inconsistent extraction row counts, and uncertain shading prevent a pass. `needs_review` can include confirmed field issues as well as extraction warnings. OCR can still misread content or omit a row without detecting the omission; inspect the original form and keep the required two-person review. The supplied examples are smoke tests, not a measured accuracy benchmark on unseen judge PDFs.

If schema errors are limited to unrecognized section names or missing cell states, the reviewer displays a partial report. Unrecognized sections are skipped. Cells missing their filled/blank/uncertain state keep their extracted text and are marked uncertain for manual verification. Page-specific warnings describe these omissions, and the incomplete transcription prevents an automatic pass. Invalid JSON and other schema errors still stop the review.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `LLAMA_PARSE_API_KEY` | required | LlamaCloud API key; `LLAMA_CLOUD_API_KEY` is an alias. |
| `LLAMA_PARSE_TIER` | `agentic` | `agentic` or `agentic_plus`. |
| `LLAMA_PARSE_VERSION` | `latest` | Can be pinned to a dated version supported by your tier. |
| `LLAMA_PARSE_TIMEOUT` | `300` | Maximum parser polling time in seconds. |
| `MAX_UPLOAD_MB` | `20` | PDF size limit. |
| `MAX_PAGES` | `10` | Upload page limit. |
| `REVIEW_TTL_SECONDS` | `3600` | Time after completion/failure before local files/results expire (minimum 60). |
| `PORT` | `8000` | Container HTTP port; hosting platforms may override it. |

Two background jobs run concurrently, with at most 24 retained/queued reviews. Jobs and uploads are local to one process. Run **one Uvicorn worker and one service instance**; restarts clear active reviews and results. The browser polls job status so long parsing does not occupy a single long HTTP request. Local cleanup runs every 30 seconds. LlamaCloud's handling of submitted documents is governed by that provider; local expiry does not delete its retained parse results.

## Deploy for the judges

The app is prepared for deployment; no public service is created automatically.

### Render (Docker web service)

1. Push this project to a Git repository, excluding `.env` and `.review-dev`.
2. In Render, create a **Web Service** from the repository and select **Docker**.
3. Use the root `Dockerfile`. Leave the Docker command at its default.
4. Add `LLAMA_PARSE_API_KEY` as a secret environment variable. Optionally set the other parser settings above.
5. Set the health check path to `/healthz`. Keep one instance. The server binds to `0.0.0.0` and honors Render's `PORT`.
6. Deploy. Open the assigned HTTPS URL, verify `parser_configured` is true at `/healthz`, upload each sample, and then test a new PDF. Give that URL to the judges.

See Render's [Docker deployment documentation](https://render.com/docs/docker) and [web-service settings](https://render.com/docs/web-services). Choose a service plan that stays available during judging. API parser charges are separate from hosting.

### Your own server

Copy the source to a Docker-enabled server, configure `.env`, and run `docker compose up --build -d`. Put your HTTPS reverse proxy in front of port 8000. Allow an upload body of at least 21 MB. Keep a single application instance. Test through the public URL, not only localhost, before submitting it.

This is an unauthenticated hackathon application for synthetic forms. If the URL is public, anyone with access can submit a parse that uses your API credits; use the hosting platform's access controls if you want to limit access to judges.

## API

- `POST /api/reviews`: multipart `file`; returns HTTP 202 with `id` and `status_url`.
- `GET /api/reviews/{id}`: `queued`, `processing`, `completed`, or `failed`; completed jobs include the report.
- `GET /api/reviews/{id}/pages/{page}`: original page rendered as PNG.
- `GET /api/reviews/{id}/report`: download the completed JSON report.
- `GET /healthz`: process health and whether a parser key is configured (does not make a paid API request).

## Verification

Run the offline rules, API, and parser-adapter tests inside the built container (PowerShell):

```powershell
docker run --rm -v "${PWD}:/workspace:ro" -w /workspace --entrypoint python regenmed-reviewer:latest -m unittest discover -s tests -v
```

Live smoke testing of the supplied synthetic examples **uses LlamaParse credits**:

```powershell
docker compose run --rm -e PYTHONPATH=/workspace -v "${PWD}:/workspace" --entrypoint python reviewer /workspace/scripts/smoke_samples.py
```

The script writes reports to ignored `.review-dev/` for inspection. Unit tests use synthetic transcriptions and mocked cloud responses; they do not call the provider.

Core files: `app.py` (HTTP/jobs), `llama_parse.py` (provider), `reviewer/prompts.py` (transcription contract), `reviewer/models.py` (schema), `reviewer/rules.py` (checks), and `static/` (web UI).



docker build -t pdf-parser .
docker run --env-file .env -p 8000:8000 pdf-parser
