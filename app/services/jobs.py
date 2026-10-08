"""Job-level operations used by the API layer (create, read, retry, export)."""

from __future__ import annotations

import csv
import io
import tempfile
import zipfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Certificate, CertificateStatus, GenerationJob, JobStatus
from app.schemas import (
    CertificateInfo,
    CertificateOut,
    JobCounts,
    JobOut,
)
from app.services.processor import status_counts
from app.services.storage import safe_filename
from app.services.validation import ValidationResult

API_PREFIX = "/api/v1"


class JobConflictError(Exception):
    """The requested operation is not allowed in the job's current state."""


# --------------------------------------------------------------------------- #
# Create
# --------------------------------------------------------------------------- #
def create_job(
    session: Session, info: CertificateInfo, result: ValidationResult, total_recipients: int
) -> GenerationJob:
    """Persist the job + one row per recipient (valid rows as ``pending``,
    invalid rows as ``invalid`` with their error messages).

    Everything is written in a single transaction, so a job is never visible
    with only some of its recipients.
    """

    job = GenerationJob(
        title=info.title,
        course_name=info.course_name,
        issuer_name=info.issuer_name,
        issue_date=info.issue_date,
        description=info.description,
        total_recipients=total_recipients,
        status=JobStatus.PENDING,
    )
    session.add(job)
    session.flush()  # assigns job.id

    certificates = [
        Certificate(
            job_id=job.id,
            row_index=index,
            recipient_name=recipient.name,
            recipient_email=recipient.email,
            status=CertificateStatus.PENDING,
        )
        for index, recipient in result.valid
    ]
    certificates += [
        Certificate(
            job_id=job.id,
            row_index=err.row_index,
            recipient_name=_maybe_str(err.input, "name", 100),
            recipient_email=_maybe_str(err.input, "email", 320),
            raw_input=err.input,  # came from JSON/CSV, so it's JSON-serialisable
            status=CertificateStatus.INVALID,
            error_message="; ".join(err.errors),
        )
        for err in result.invalid
    ]
    session.add_all(certificates)
    session.commit()
    return job


def _maybe_str(raw: object, key: str, max_len: int) -> str | None:
    if isinstance(raw, dict) and isinstance(raw.get(key), str):
        return raw[key][:max_len]
    return None


# --------------------------------------------------------------------------- #
# Read / serialise
# --------------------------------------------------------------------------- #
def job_to_out(session: Session, job: GenerationJob) -> JobOut:
    counts = status_counts(session, job.id)
    processable = job.total_recipients - counts[CertificateStatus.INVALID]
    finished = counts[CertificateStatus.GENERATED] + counts[CertificateStatus.FAILED]
    progress = round(100 * finished / processable, 2) if processable else 100.0

    base = f"{API_PREFIX}/jobs/{job.id}"
    return JobOut(
        id=job.id,
        status=job.status,
        certificate=CertificateInfo(
            title=job.title,
            course_name=job.course_name,
            issuer_name=job.issuer_name,
            issue_date=job.issue_date,
            description=job.description,
        ),
        counts=JobCounts(
            total=job.total_recipients,
            pending=counts[CertificateStatus.PENDING],
            generated=counts[CertificateStatus.GENERATED],
            failed=counts[CertificateStatus.FAILED],
            invalid=counts[CertificateStatus.INVALID],
        ),
        progress_percent=progress,
        error_message=job.error_message,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        links={
            "self": base,
            "certificates": f"{base}/certificates",
            "failed": f"{base}/certificates?status=failed",
            "download_zip": f"{base}/download",
        },
    )


def certificate_to_out(cert: Certificate) -> CertificateOut:
    generated = cert.status == CertificateStatus.GENERATED
    return CertificateOut(
        id=cert.id,
        job_id=cert.job_id,
        row_index=cert.row_index,
        status=cert.status,
        recipient_name=cert.recipient_name,
        recipient_email=cert.recipient_email,
        verification_code=cert.verification_code if generated else None,
        error_message=cert.error_message,
        attempts=cert.attempts,
        generated_at=cert.generated_at,
        download_url=f"{API_PREFIX}/certificates/{cert.id}/download" if generated else None,
        raw_input=cert.raw_input if cert.status == CertificateStatus.INVALID else None,
    )


# --------------------------------------------------------------------------- #
# Retry
# --------------------------------------------------------------------------- #
def reset_failed_for_retry(session: Session, job: GenerationJob) -> int:
    """Move failed certificates back to pending. Returns how many were reset."""
    if job.status in (JobStatus.PENDING, JobStatus.PROCESSING):
        raise JobConflictError("Job is still running; wait for it to finish before retrying.")

    failed = session.scalars(
        select(Certificate).where(
            Certificate.job_id == job.id, Certificate.status == CertificateStatus.FAILED
        )
    ).all()
    if not failed:
        raise JobConflictError("Job has no failed certificates to retry.")

    for cert in failed:
        cert.status = CertificateStatus.PENDING
        cert.error_message = None
    job.status = JobStatus.PENDING
    job.completed_at = None
    session.commit()
    return len(failed)


def unfinished_job_ids(session: Session) -> list[str]:
    """Jobs interrupted by a shutdown/crash. 'processing' ones are reset to 'pending'."""
    jobs = session.scalars(
        select(GenerationJob)
        .where(GenerationJob.status.in_([JobStatus.PENDING, JobStatus.PROCESSING]))
        .order_by(GenerationJob.created_at)
    ).all()
    for job in jobs:
        job.status = JobStatus.PENDING
    session.commit()
    return [job.id for job in jobs]


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #
def build_job_zip(session: Session, job: GenerationJob) -> Path:
    """ZIP every generated PDF plus a ``manifest.csv`` describing every row
    (including failed / invalid ones, with their error messages)."""
    certs = session.scalars(
        select(Certificate).where(Certificate.job_id == job.id).order_by(Certificate.row_index)
    ).all()
    if not any(c.status == CertificateStatus.GENERATED for c in certs):
        raise JobConflictError("No certificates have been generated for this job yet.")

    tmp = tempfile.NamedTemporaryFile(prefix=f"job-{job.id}-", suffix=".zip", delete=False)
    tmp.close()
    manifest = io.StringIO()
    writer = csv.writer(manifest)
    writer.writerow(
        ["row_index", "recipient_name", "recipient_email", "status", "certificate_id",
         "verification_code", "file", "error"]
    )
    with zipfile.ZipFile(tmp.name, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for cert in certs:
            arcname = ""
            has_file = cert.file_path is not None and Path(cert.file_path).is_file()
            if cert.status == CertificateStatus.GENERATED and has_file:
                arcname = f"{cert.row_index:05d}_{safe_filename(cert.recipient_name or '')}.pdf"
                zf.write(cert.file_path, arcname=f"certificates/{arcname}")
            writer.writerow(
                [cert.row_index, cert.recipient_name or "", cert.recipient_email or "",
                 cert.status.value, cert.id,
                 cert.verification_code if cert.status == CertificateStatus.GENERATED else "",
                 f"certificates/{arcname}" if arcname else "", cert.error_message or ""]
            )
        zf.writestr("manifest.csv", manifest.getvalue())
    return Path(tmp.name)


def parse_recipients_csv(content: bytes) -> list[dict[str, str]]:
    """Parse an uploaded CSV with (at least) ``name`` and ``email`` headers."""
    try:
        text = content.decode("utf-8-sig")  # tolerate Excel's BOM
    except UnicodeDecodeError as exc:
        raise ValueError("CSV must be UTF-8 encoded") from exc
    reader = csv.DictReader(io.StringIO(text))
    headers = {h.strip().lower() for h in (reader.fieldnames or [])}
    missing = {"name", "email"} - headers
    if missing:
        raise ValueError(f"CSV is missing required column(s): {', '.join(sorted(missing))}")
    rows = []
    for row in reader:
        normalised = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        if not any(normalised.values()):
            continue  # skip fully blank lines
        rows.append(normalised)
    if not rows:
        raise ValueError("CSV contains no recipient rows")
    return rows
