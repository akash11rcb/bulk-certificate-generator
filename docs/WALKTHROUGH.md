# Code Walkthrough & Change Guide

A guided tour of the codebase: how one request flows through it, the questions a reviewer is
likely to ask, and where to make the most probable requirement changes.

---

## 1. Life of a request

```
Client                API (routes.py)                services                       DB / disk
  │  POST /api/v1/jobs     │                              │                              │
  ├───────────────────────►│ Pydantic validates envelope  │                              │
  │                        │ (CreateJobRequest) ─ 422 if  │                              │
  │                        │ malformed                    │                              │
  │                        │ size check ─ 422 if too many │                              │
  │                        │ validate_recipients() ──────►│ row-by-row RecipientIn,      │
  │                        │                              │ duplicate emails             │
  │                        │ allow_partial / no-valid ─ 422                              │
  │                        │ jobs.create_job() ──────────►│ 1 job + N certificate rows ─►│ (1 transaction)
  │                        │ runner.submit(job_id) ──────►│ ThreadPool queues job        │
  │◄── 202 + Location ─────┤                              │                              │
  │                        │                     [worker thread]                         │
  │                        │                     JobProcessor.process(job_id)            │
  │                        │                       _claim(): UPDATE … WHERE pending ────►│
  │                        │                       loop pending certs (batches of 100):  │
  │                        │                         _generate_one() → render_certificate│──► tmp file → os.replace
  │                        │                         status generated/failed, commit ───►│ (per certificate)
  │                        │                       final status from counts ────────────►│
  │  GET /jobs/{id}        │ job_to_out(): GROUP BY status → counts, progress %          │
  ├───────────────────────►│◄────────────────────────────────────────────────────────────│
  │  GET /jobs/{id}/download → build_job_zip(): PDFs + manifest.csv → temp zip → stream  │
```

### Files in reading order

1. `app/schemas.py`: what the API accepts and returns.
2. `app/services/validation.py`: per-row validation (~60 lines).
3. `app/api/routes.py` → `_submit()`: the create flow.
4. `app/services/jobs.py` → `create_job()`, `job_to_out()`.
5. `app/services/processor.py`: **the core**. Claim, loop, isolate failures, finalise.
6. `app/services/runner.py`: how the processor is executed (thread pool vs inline).
7. `app/main.py`: wiring, plus resume-on-startup and graceful stop.
8. `app/services/renderer.py`: the PDF template.

---

## 2. Likely questions, with short answers

**Why background processing instead of synchronous?**
Request time scales with the list (~10 ms per certificate, so 5,000 ≈ 50 s), which runs into
HTTP/proxy timeouts. With a background job the POST returns in ~0.1 s, progress can be polled, and
a crash affects at most one certificate instead of the whole request.

**Why not Celery?**
It would add a broker (Redis/RabbitMQ) and a second process to run, which this scale doesn't
need. State lives in the DB, not the runner, so switching later means writing one `CeleryJobRunner`
whose `submit()` calls `process_job.delay(job_id)`.

**What happens if the server crashes mid-job?**
Each certificate's status is committed as soon as it's done. On restart, `unfinished_job_ids()`
resets `processing` jobs to `pending`, and they're re-submitted. The processor only picks up
`pending` certificates, so it continues where it stopped. The one certificate that was in flight is
re-rendered, which is safe because writes are atomic and go to the same path.

**How do you stop two workers processing the same job?**
`_claim()` runs `UPDATE generation_jobs SET status='processing' WHERE id=? AND status='pending'`.
The database applies that atomically, so only one worker sees `rowcount == 1`.

**Why are counts computed instead of stored?**
Stored counters need careful concurrent increments and can drift. A `GROUP BY` on the
`(job_id, status)` index is cheap and always correct.

**Why is `recipients` typed as `list[Any]`?**
If it were `list[RecipientIn]`, Pydantic would reject the whole request on the first bad row.
Validating rows one by one gives row-level errors and allows partial success.

**What's the difference between `invalid` and `failed`?**
`invalid` means the input was rejected before generation and retrying won't help. `failed`
means generation was attempted and errored, and it can be retried.

**How do I simulate a failure?**
Tests inject a renderer: `create_app(..., renderer=failing_renderer({"Person 2"}))`. For a
live server, submit a CJK name such as `李雷`. It passes validation but the template font
can't draw it.

**Why is SQLite fine here? Is it really relational?**
Yes, it's a relational SQL database. WAL mode lets API reads continue while a worker writes.
Setting `DATABASE_URL` switches to PostgreSQL with no code changes.

**What's the complexity of one job?**
O(N) renders plus O(N) small commits. Memory is bounded because the processor loads pending
certificates in batches of 100.

---

## 3. Probable requirement changes and where to make them

| Change | Where | Notes |
|---|---|---|
| **Add a recipient field** (e.g. `grade`) printed on the certificate | `schemas.RecipientIn` → `models.Certificate` (new column) → `jobs.create_job` (save it) → `renderer.CertificateData` + draw it → `processor._generate_one` (pass it) | Add a test in `test_generation.py` that checks the text is in the PDF. |
| **Add a shared field** (e.g. instructor) | `schemas.CertificateInfo` → `models.GenerationJob` → `jobs.create_job` / `job_to_out` → renderer | Same pattern. |
| **Reject the whole request on any invalid row by default** | Change the `allow_partial` default to `False` in `CreateJobRequest` | Logic already exists. |
| **Allow duplicate emails** | Remove the `seen_emails` block in `validation.py` | Update `test_duplicate_emails…`. |
| **Automatic retries** for transient failures | `processor._generate_one`: loop up to `MAX_ATTEMPTS`, retry only non-`RenderError` exceptions, maybe with backoff | `attempts` is already tracked. |
| **Run synchronously** for small jobs | In `_submit`: `if len(result.valid) <= 20: processor.process(job.id) else runner.submit(job.id)` | Response then already shows `completed`. |
| **Generate certificates in parallel within a job** | Render in a `ProcessPoolExecutor` inside `_run`, with the main thread doing DB updates | Keep DB writes on one thread; SQLite allows one writer. |
| **Multiple templates** | Add `template_id` to `CertificateInfo`/`GenerationJob`, keep a `TEMPLATES = {"classic": render_classic, …}` registry in the renderer, look it up in `_generate_one` | Validate `template_id` with an `Enum`/`Literal`. |
| **PNG instead of / as well as PDF** | Renderer: rasterise with `pypdfium2`, or draw with Pillow; storage `path_for` takes an extension | |
| **Email certificates to recipients** | After `GENERATED`, enqueue an email task; add an `emailed_at` column | Keep it separate from generation so email failures don't mark certificates failed. |
| **Webhook when a job finishes** | Add `callback_url` to the request/job; POST to it at the end of `_run` (separate try/except, with retries) | |
| **Cancel a job** | `POST /jobs/{id}/cancel` sets status `cancelled`; in the `_run` loop, re-read the job status per batch and stop if cancelled | Add `CANCELLED` to `JobStatus`. |
| **Delete a job and its files** | `DELETE /jobs/{id}`: `shutil.rmtree(storage.root / job_id)` + `session.delete(job)` (cascade removes certificates) | Return 409 if processing. |
| **Use PostgreSQL** | `DATABASE_URL=postgresql+psycopg://…`, `pip install "psycopg[binary]"` | No code change. |

---

## 4. Debugging tips

- **Server logs** show each job's start and finish with counts (`Job … finished: completed_with_errors (generated=4 failed=1 invalid=3)`), and a warning per failed certificate.
- **Inspect the DB:** `sqlite3 data/certificates.db "select row_index,status,error_message from certificates where job_id='…'"`.
- **Run one test:** `pytest tests/test_failure_handling.py::test_retry_regenerates_only_failed_certificates -v`.
- **Run tests with output:** `pytest -s -v`.
- **Swagger UI** (`/docs`) is the quickest way to send requests by hand.
