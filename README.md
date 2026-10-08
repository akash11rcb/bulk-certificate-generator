# Bulk Certificate Generator — Backend API

A FastAPI service that accepts **one request containing many recipients**, validates each
recipient, generates a **PDF certificate per valid recipient** from a single predefined
template in the **background**, tracks **per-certificate status and job progress**, and lets
clients **download** certificates individually or as a ZIP.

<p align="center"><img src="docs/sample-certificate.png" alt="Sample generated certificate" width="720"></p>

| | |
|---|---|
| **Language / framework** | Python 3.11+ · FastAPI · Pydantic v2 |
| **Database** | SQLite by default (relational, zero setup) · any SQLAlchemy URL works, e.g. PostgreSQL |
| **ORM** | SQLAlchemy 2.0 |
| **PDF generation** | ReportLab, with a bundled Unicode font (DejaVu Serif) |
| **Background processing** | In-process thread pool behind a small `JobRunner` interface |
| **Tests** | pytest — 42 tests, ~3 s |

---

## Contents

1. [Quick start](#1-quick-start)
2. [Project structure](#2-project-structure)
3. [Running the tests](#3-running-the-tests)
4. [API overview](#4-api-overview)
5. [Submitting a certificate generation request](#5-submitting-a-certificate-generation-request)
6. [Tracking progress](#6-tracking-progress)
7. [Retrieving generated certificates](#7-retrieving-generated-certificates)
8. [Design decisions](#8-design-decisions)
9. [Configuration](#9-configuration)
10. [Limitations and next steps](#10-limitations-and-next-steps)

---

## 1. Quick start

**Prerequisites:** Python 3.11 or newer.

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate

# 2. Install dependencies (runtime + test tools)
pip install -r requirements-dev.txt

# 3. Run the API
uvicorn app.main:app --reload
```

The API is now on **http://localhost:8000**. Interactive Swagger docs: **http://localhost:8000/docs**.

The SQLite database and generated PDFs are created automatically under `./data/` on first start.
No migrations or manual DB setup are needed.

**End-to-end demo** (in a second terminal, while the server is running):

```bash
python scripts/demo.py
```

It submits [`examples/request.json`](examples/request.json) (which deliberately contains valid,
invalid, duplicate and un-renderable recipients), polls until the job finishes, prints every
recipient's result, and downloads the ZIP into `./downloads/`. Sample output:

```
Job 1624…dab accepted: 5 valid, 3 rejected
  ✗ row 4: name: must not be blank
  ✗ row 5: email: value is not a valid email address: An email address must have an @-sign.
  ✗ row 6: email: duplicate of recipient at row 1
  status=processing             progress=   0.0%  {'total': 8, 'pending': 5, 'generated': 0, 'failed': 0, 'invalid': 3}
  status=completed_with_errors  progress= 100.0%  {'total': 8, 'pending': 0, 'generated': 4, 'failed': 1, 'invalid': 3}

Per-recipient results:
  [0] generated Ada Lovelace           /api/v1/certificates/54e6…/download
  [1] generated Alan Turing            /api/v1/certificates/27b8…/download
  [2] generated Zoë Ñúñez              /api/v1/certificates/ffb8…/download
  [3] generated Grace Hopper           /api/v1/certificates/b40c…/download
  [4] invalid   -                      name: must not be blank
  [5] invalid   Bad Email              email: value is not a valid email address: …
  [6] invalid   Alan Turing (again)    email: duplicate of recipient at row 1
  [7] failed    李雷                    recipient name contains characters the certificate font cannot render: 李 雷
```

**Docker (optional):**

```bash
docker build -t cert-generator .
docker run -p 8000:8000 -v "$(pwd)/data:/app/data" cert-generator
```

---

## 2. Project structure

```
app/
├── main.py              # App factory: wiring, startup recovery, graceful shutdown
├── config.py            # Settings from environment variables
├── database.py          # SQLAlchemy engine / session (SQLite pragmas: WAL, FKs)
├── models.py            # GenerationJob + Certificate tables, status enums
├── schemas.py           # Pydantic request/response models + field validation
├── api/
│   ├── deps.py          # FastAPI dependencies (session, runner, 404 lookups)
│   └── routes.py        # All HTTP endpoints
├── services/
│   ├── validation.py    # Per-recipient validation (row-level errors, duplicates)
│   ├── jobs.py          # Create / serialise / retry / ZIP export / CSV parsing
│   ├── processor.py     # Background job processing — failure isolation lives here
│   ├── runner.py        # JobRunner: ThreadPool (prod) / Inline (tests)
│   ├── renderer.py      # The predefined certificate template (ReportLab → PDF)
│   └── storage.py       # Local file storage + safe filenames
└── assets/fonts/        # DejaVu Serif (Unicode coverage) + licence
tests/                   # pytest suite (see §3)
examples/                # request.json, recipients.csv
scripts/demo.py          # End-to-end client (stdlib only)
docs/                    # Sample certificate (PNG + PDF), WALKTHROUGH.md
```

The layering is deliberate: **routes** handle HTTP only, **services** hold the logic, and
**processor** knows nothing about HTTP. That keeps each piece testable on its own and makes the
background mechanism swappable.

---

## 3. Running the tests

```bash
pytest            # or: python -m pytest -v
```

Each test gets its own temporary SQLite database and storage folder. Most tests use an
**inline runner** (jobs execute synchronously during the request), so they assert on the final
state with no sleeping or polling. A separate test exercises the **real thread pool**.

| Requirement | Where it is tested |
|---|---|
| Creating a generation job | `test_create_job.py` — 202 + `Location` header, counts, runner hand-off, CSV upload |
| Input validation | `test_validation.py` (unit: blank/missing/invalid fields, duplicates, control characters, length) · `test_create_job.py` (API: `allow_partial`, empty list, size limit, bad certificate fields, CSV columns) |
| Certificate generation | `test_generation.py` — PDF opens, is landscape, contains the recipient's name/course/date/ID; Unicode names; long names; one PDF per recipient |
| Job status / progress | `test_job_status.py` — pending → processing → completed, live progress mid-job (0/25/50/75 %), invalid rows excluded from progress, pagination, real background thread, no double-processing, resume after restart |
| Individual certificate failure | `test_failure_handling.py` — one failure doesn't block others, unexpected exceptions isolated, real font failure, all-fail → `failed`, retry only failed ones, job-level crash recorded |
| Retrieving certificates | `test_retrieval.py` — single PDF download, metadata, 409 for not-yet/failed, 404s, ZIP contents + manifest, public verification |

---

## 4. API overview

All endpoints are under `/api/v1`. Full schemas are in Swagger at `/docs`.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/jobs` | Submit a bulk job (JSON). Returns **202 Accepted**. |
| `POST` | `/jobs/upload` | Submit a bulk job with recipients from a **CSV file** (multipart). |
| `GET` | `/jobs` | List jobs, newest first (`?status=&limit=&offset=`). |
| `GET` | `/jobs/{job_id}` | Job status, counts and progress %. |
| `GET` | `/jobs/{job_id}/certificates` | Per-recipient results (`?status=generated\|failed\|invalid\|pending`, paginated). |
| `GET` | `/jobs/{job_id}/download` | ZIP of all generated PDFs + `manifest.csv`. |
| `POST` | `/jobs/{job_id}/retry` | Re-run generation for the job's **failed** certificates only. |
| `GET` | `/certificates/{id}` | One certificate's metadata. |
| `GET` | `/certificates/{id}/download` | The certificate PDF. |
| `GET` | `/verify/{code}` | Public check that a printed certificate ID is genuine. |
| `GET` | `/health` | Liveness probe. |

**Status codes used:** `202` job accepted · `404` unknown job/certificate · `409` action not
possible in the current state (e.g. downloading a certificate that isn't generated yet, retrying a
running job) · `410` file missing from storage · `422` invalid request.

---

## 5. Submitting a certificate generation request

### JSON

```bash
curl -X POST http://localhost:8000/api/v1/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "certificate": {
      "title": "Certificate of Completion",
      "course_name": "Advanced Python",
      "issuer_name": "Acme Academy",
      "issue_date": "2026-10-08",
      "description": "Awarded for completing 40 hours of coursework."
    },
    "recipients": [
      {"name": "Ada Lovelace", "email": "ada@example.com"},
      {"name": "Alan Turing",  "email": "alan@example.com"},
      {"name": "",             "email": "oops@example.com"}
    ],
    "allow_partial": true
  }'
```

Or use the bundled example: `curl -X POST localhost:8000/api/v1/jobs -H "Content-Type: application/json" -d @examples/request.json`

| Field | Required | Notes |
|---|---|---|
| `certificate.course_name` | ✔ | 1–200 chars |
| `certificate.issuer_name` | ✔ | 1–120 chars |
| `certificate.title` | | Default `"Certificate of Completion"` |
| `certificate.issue_date` | | `YYYY-MM-DD`, default today |
| `certificate.description` | | ≤ 300 chars, printed under the course name |
| `recipients[].name` | ✔ | 1–100 chars, must contain a letter; whitespace is normalised |
| `recipients[].email` | ✔ | Valid email; must be unique within the job (case-insensitive) |
| `allow_partial` | | Default `true`. See [validation strategy](#82-validation-strategy). |

**Response — `202 Accepted`** (with a `Location: /api/v1/jobs/{id}` header):

```json
{
  "job": {
    "id": "3f0c…",
    "status": "pending",
    "counts": {"total": 3, "pending": 2, "generated": 0, "failed": 0, "invalid": 1},
    "progress_percent": 0.0,
    "links": {
      "self": "/api/v1/jobs/3f0c…",
      "certificates": "/api/v1/jobs/3f0c…/certificates",
      "failed": "/api/v1/jobs/3f0c…/certificates?status=failed",
      "download_zip": "/api/v1/jobs/3f0c…/download"
    }
  },
  "accepted": 2,
  "rejected": 1,
  "rejected_recipients": [
    {"row_index": 2, "input": {"name": "", "email": "oops@example.com"}, "errors": ["name: must not be blank"]}
  ]
}
```

### CSV upload

```bash
curl -X POST http://localhost:8000/api/v1/jobs/upload \
  -F "file=@examples/recipients.csv" \
  -F "course_name=Data Engineering" \
  -F "issuer_name=Acme Academy"
```

The CSV needs a header row with `name` and `email` (case-insensitive; extra columns are ignored,
Excel's UTF-8 BOM is handled, blank lines skipped). Optional form fields: `title`, `issue_date`,
`description`, `allow_partial`.

---

## 6. Tracking progress

```bash
curl http://localhost:8000/api/v1/jobs/{job_id}
```

```json
{
  "id": "3f0c…",
  "status": "processing",
  "counts": {"total": 502, "pending": 210, "generated": 289, "failed": 1, "invalid": 2},
  "progress_percent": 58.0,
  "started_at": "2026-10-08T05:50:39.926432Z",
  "completed_at": null
}
```

**Job statuses**

| Status | Meaning |
|---|---|
| `pending` | Accepted, waiting for a worker |
| `processing` | Certificates are being generated |
| `completed` | Every valid recipient has a certificate |
| `completed_with_errors` | Some generated, some failed — see `?status=failed` |
| `failed` | No certificate could be generated, or the job itself crashed (`error_message` explains) |

**Certificate statuses:** `pending` → `generated` or `failed`; `invalid` rows were rejected at
validation and are never generated.

`progress_percent` = (generated + failed) / (total − invalid). Invalid rows are excluded so a job
with rejected rows still reaches 100 %.

To see exactly which recipients failed and why:

```bash
curl "http://localhost:8000/api/v1/jobs/{job_id}/certificates?status=failed"
```

---

## 7. Retrieving generated certificates

```bash
# All certificates of a job as a ZIP (PDFs + manifest.csv)
curl -o certificates.zip http://localhost:8000/api/v1/jobs/{job_id}/download

# List results with per-certificate download links
curl "http://localhost:8000/api/v1/jobs/{job_id}/certificates?status=generated"

# One certificate PDF
curl -OJ http://localhost:8000/api/v1/certificates/{certificate_id}/download

# Verify a certificate by the ID printed on it (public)
curl http://localhost:8000/api/v1/verify/U75LMXVHVUE3
```

The ZIP contains `certificates/00000_ada-lovelace.pdf` etc. (prefixed with the input row so the
order matches the submitted list) and a **`manifest.csv`** with one line per submitted recipient —
including failed and invalid ones with their error messages — so the result can be reconciled
offline against the original list.

---

## 8. Design decisions

### 8.1 Background processing (and why)

**Decision:** `POST /jobs` validates and stores the job, hands the job ID to a background worker,
and returns **202 Accepted** immediately. Clients poll `GET /jobs/{id}`.

**Reasoning:**

- **Request duration grows with the list.** Measured locally, one certificate takes about
  **10 ms** to render. A 500-recipient job finishes in ~5 s, but 5,000 recipients would take
  close to a minute. That is longer than typical proxy/load-balancer timeouts (often 30–60 s),
  and a dropped connection would leave the client with no idea what happened. Running in the
  background, the POST returns in ~0.1 s whatever the list size.
- **Progress becomes observable.** Since state lives in the DB, the client can poll and see
  partial results.
- **Failures are isolated from the HTTP request.** A rendering problem never turns into a 500
  for the whole submission.

**Why an in-process thread pool rather than Celery/RQ?** It needs no extra infrastructure
(no Redis or broker), so the project runs with a single command, and it's enough for the
assignment's scale. The important part is that **all job state is in the database, not in the
runner**. `JobRunner` is a two-method interface (`submit`, `shutdown`), so moving to Celery is one
new class whose `submit` enqueues the job ID. The processor, API and tests stay the same.

Trade-offs I accept with this choice (and how they're mitigated):

| Concern | Mitigation |
|---|---|
| Process restarts mid-job | Progress is committed after **each** certificate. On startup, unfinished jobs are re-queued and continue from their remaining `pending` certificates (`unfinished_job_ids`). |
| Shutdown while jobs run | Lifespan shutdown calls `processor.request_stop()`. Workers stop after their current certificate and put the job back to `pending` for the next start. |
| Same job processed twice | Workers **claim** a job with a conditional `UPDATE … WHERE status='pending'`. Only one claim can succeed. |
| CPU-bound rendering + the GIL | Threads give concurrency between jobs, not parallel CPU. For heavy load, run separate worker processes (Celery) or a `ProcessPoolExecutor`. |
| Multiple API processes (`--workers N`) | The in-process runner assumes a single API process. With several, use a real queue (see §10). |

### 8.2 Validation strategy

Validation happens in **two layers**:

1. **Request envelope** (Pydantic): `certificate` fields, `recipients` must be a non-empty list,
   size limit (`MAX_RECIPIENTS_PER_JOB`, default 5,000). If any of these fail, the request is
   malformed and the whole thing is rejected with **422**.
2. **Each recipient, individually** (`services/validation.py`): `recipients` is typed loosely
   (`list[Any]`) on purpose, and each row is validated separately against `RecipientIn`. If the
   list were typed strictly, one bad email in 1,000 rows would reject the entire request.

Recipient rules: name required, trimmed with whitespace collapsed, 1–100 chars, must contain a
letter, no control characters. Email must be valid and is lower-cased. **Duplicate emails within a
job are rejected** (later occurrences), so nobody receives two identical certificates.

**`allow_partial`** lets the client choose:

- `true` (default): invalid rows are skipped and reported in `rejected_recipients` (with
  `row_index`, original input and reasons). Valid rows proceed.
- `false`: all-or-nothing. Any invalid row returns **422** listing *all* problems, and nothing
  is persisted.

If no row is valid, the request is always rejected with 422, since there's nothing to generate.

Invalid rows are **stored** too (status `invalid`, with error and raw input). That way the job
record is a complete account of what was submitted, and the ZIP manifest can list them.

### 8.3 Failure handling

- **Per-certificate isolation.** In `JobProcessor._generate_one`, each render runs in its own
  `try/except`. A failure sets that certificate to `failed` with an `error_message`, and the loop
  moves on. Expected problems (`RenderError`) store the message as-is. Unexpected exceptions are
  logged with a traceback and stored as `ExceptionType: message`.
- **Validation errors vs generation failures are different statuses.** `invalid` means the input
  was bad and can't be retried. `failed` means generation broke and can be retried.
- **A real failure mode is handled, not just simulated.** A name can be valid input but
  contain characters the template font can't draw (e.g. CJK with DejaVu). The renderer checks
  glyph coverage *before* drawing and fails with a clear message, instead of producing a PDF with
  blank boxes.
- **Final job status** comes from the counts: no failures → `completed`; some → `completed_with_errors`;
  none generated → `failed`.
- **Job-level crashes** (e.g. the DB becomes unavailable) are caught in `process()`. The job is
  marked `failed` with the error, so it never stays stuck in `processing`.
- **Retry:** `POST /jobs/{id}/retry` moves only `failed` certificates back to `pending` and
  re-queues the job. Generated certificates aren't touched, and `attempts` records how often each
  was tried.
- **No partial files.** PDFs are written to a temp file and atomically `os.replace`d into place.
  A crash mid-render can't leave a corrupt certificate behind, and re-rendering is idempotent.

### 8.4 Data model

```
generation_jobs                         certificates
───────────────                         ────────────
id (uuid) PK                            id (uuid) PK
status                                  job_id FK → generation_jobs.id  ┐ composite index
title, course_name, issuer_name,        status                           ┘ (job_id, status)
issue_date, description                 row_index      (position in submitted list)
total_recipients                        recipient_name, recipient_email
error_message                           raw_input      (JSON, for invalid rows)
created_at, started_at, completed_at    verification_code (unique, printed on PDF)
                                        file_path, error_message, attempts, generated_at
```

- **Counts are computed** (`GROUP BY status` on the `(job_id, status)` index), not stored as
  counters. Concurrent updates therefore can't make them drift.
- **Shared certificate info lives on the job**, not copied onto every row.
- **`row_index`** maps every result back to the client's original input.
- **UUIDs** for public IDs, so they can't be enumerated.
- Timestamps are stored and returned as **UTC with timezone** (a small `UTCDateTime` type, since
  SQLite drops timezones).
- Tables are created at startup with `create_all` to keep setup simple. Alembic is noted under
  next steps.

### 8.5 The certificate template

- One template, drawn in code with ReportLab (`services/renderer.py`): A4 landscape, navy/gold
  frame, issuer, title, recipient name, course, optional description, date, signature line, seal,
  and a footer with the **certificate ID and verification URL**.
- **Unicode-safe:** bundled DejaVu Serif covers Latin (incl. accents), Greek and Cyrillic. If the
  TTFs are missing it falls back to built-in Times with a cp1252 check.
- **Layout-safe:** long names and course names shrink to fit and wrap to two lines if needed.
  Anything longer is truncated with an ellipsis, so the layout never breaks.
- PDF metadata (title, author, subject) is set. Page compression is on (~58 KB per PDF, mostly
  the embedded font subsets).
- **Verification codes** use an unambiguous 32-character alphabet (no `0/O`, `1/I`) and are
  checked case-insensitively, so they're easy to type from paper.

### 8.6 Storage and security

- Files are stored at `STORAGE_DIR/{job_id}/{certificate_id}.pdf`. Paths are built **only from
  server-generated IDs** and never from user input, so path traversal isn't possible.
- Human-friendly download names (`ada-lovelace-U75L….pdf`) are created with an ASCII slug in
  `Content-Disposition` only.
- `LocalStorage` is a small class, so S3/GCS (with pre-signed download URLs) can replace it later.

---

## 9. Configuration

All settings are environment variables (see [`.env.example`](.env.example)):

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./data/certificates.db` | Any SQLAlchemy URL, e.g. `postgresql+psycopg://user:pass@host/db` (install `psycopg[binary]`) |
| `STORAGE_DIR` | `./data/certificates` | Where PDFs are written |
| `MAX_RECIPIENTS_PER_JOB` | `5000` | Upper bound per request |
| `WORKER_THREADS` | `2` | Jobs processed concurrently |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | Used in the verification URL printed on certificates |

---

## 10. Limitations and next steps

What I would add for production, roughly in priority order:

1. **Authentication / multi-tenancy:** an API key or OAuth, with jobs scoped to the caller.
2. **Dedicated workers:** Celery/RQ/Dramatiq plus a broker, so workers scale separately from the
   API and use multiple CPUs. This is a single new `JobRunner` implementation.
3. **PostgreSQL + Alembic migrations** instead of SQLite + `create_all`.
4. **Object storage** (S3/GCS) with pre-signed URLs, plus a retention policy for old files.
5. **Webhooks / callbacks** when a job finishes, as an alternative to polling.
6. **Idempotency keys** on `POST /jobs`, so a client retrying after a network error doesn't
   create a duplicate job.
7. **Very large lists** (100k+): stream CSV parsing and bulk-insert rows in chunks.
8. **Broader script coverage:** add Noto fonts with per-script fallback so CJK/Arabic names render.
9. **Observability:** structured logs, metrics (queue depth, render latency, failure rate).

See [`docs/WALKTHROUGH.md`](docs/WALKTHROUGH.md) for a step-by-step trace of a request through the
code and notes on handling likely requirement changes.
