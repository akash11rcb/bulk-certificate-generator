"""HTTP endpoints (all under /api/v1)."""

from __future__ import annotations

import os
from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.api.deps import (
    get_certificate_or_404,
    get_job_or_404,
    get_runner,
    get_session,
    get_settings,
)
from app.config import Settings
from app.models import Certificate, CertificateStatus, GenerationJob, JobStatus
from app.schemas import (
    CertificateOut,
    CertificatePage,
    CreateJobRequest,
    JobCreatedOut,
    JobOut,
    JobPage,
    VerificationOut,
)
from app.services import jobs as job_service
from app.services.runner import JobRunner
from app.services.storage import safe_filename
from app.services.validation import validate_recipients

router = APIRouter(prefix="/api/v1")


# --------------------------------------------------------------------------- #
# Create jobs
# --------------------------------------------------------------------------- #
def _submit(
    request: CreateJobRequest,
    response: Response,
    session: Session,
    runner: JobRunner,
    settings: Settings,
) -> JobCreatedOut:
    if len(request.recipients) > settings.max_recipients_per_job:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Too many recipients: {len(request.recipients)} "
                f"(maximum {settings.max_recipients_per_job} per job). Split the list into several jobs."
            ),
        )

    # Validate every recipient before touching the DB.
    result = validate_recipients(request.recipients)
    if not result.valid or (result.invalid and not request.allow_partial):
        reason = (
            "No valid recipients in request."
            if not result.valid
            else "Some recipients are invalid and allow_partial is false."
        )
        raise HTTPException(
            status_code=422,
            detail={
                "message": reason,
                "rejected_recipients": [e.model_dump() for e in result.invalid],
            },
        )

    job = job_service.create_job(session, request.certificate, result, len(request.recipients))
    runner.submit(job.id)

    session.expire_all()  # the worker may already have updated the job
    response.headers["Location"] = f"/api/v1/jobs/{job.id}"
    return JobCreatedOut(
        job=job_service.job_to_out(session, session.get(GenerationJob, job.id)),
        accepted=len(result.valid),
        rejected=len(result.invalid),
        rejected_recipients=result.invalid,
    )


@router.post(
    "/jobs",
    response_model=JobCreatedOut,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["jobs"],
    summary="Submit a bulk certificate generation job (JSON)",
)
def create_job(
    payload: CreateJobRequest,
    response: Response,
    session: Session = Depends(get_session),
    runner: JobRunner = Depends(get_runner),
    settings: Settings = Depends(get_settings),
) -> JobCreatedOut:
    """Accepts the shared certificate info and a list of recipients.

    Returns **202 Accepted** immediately; generation runs in the background.
    Poll `GET /api/v1/jobs/{id}` for progress.
    """
    return _submit(payload, response, session, runner, settings)


@router.post(
    "/jobs/upload",
    response_model=JobCreatedOut,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["jobs"],
    summary="Submit a job with recipients from a CSV file",
)
async def create_job_from_csv(
    response: Response,
    file: UploadFile = File(..., description="CSV with header row containing `name` and `email`"),
    course_name: str = Form(...),
    issuer_name: str = Form(...),
    title: str = Form("Certificate of Completion"),
    issue_date: date | None = Form(None),
    description: str | None = Form(None),
    allow_partial: bool = Form(True),
    session: Session = Depends(get_session),
    runner: JobRunner = Depends(get_runner),
    settings: Settings = Depends(get_settings),
) -> JobCreatedOut:
    try:
        recipients = job_service.parse_recipients_csv(await file.read())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    certificate = {"title": title, "course_name": course_name, "issuer_name": issuer_name}
    if issue_date:
        certificate["issue_date"] = issue_date
    if description:
        certificate["description"] = description
    try:
        payload = CreateJobRequest(
            certificate=certificate, recipients=recipients, allow_partial=allow_partial
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=422, detail=exc.errors(include_url=False, include_context=False)
        ) from exc
    return _submit(payload, response, session, runner, settings)


# --------------------------------------------------------------------------- #
# Read jobs
# --------------------------------------------------------------------------- #
@router.get("/jobs", response_model=JobPage, tags=["jobs"], summary="List jobs (newest first)")
def list_jobs(
    status_filter: JobStatus | None = Query(None, alias="status"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
) -> JobPage:
    query = select(GenerationJob)
    count_query = select(func.count()).select_from(GenerationJob)
    if status_filter:
        query = query.where(GenerationJob.status == status_filter)
        count_query = count_query.where(GenerationJob.status == status_filter)
    jobs = session.scalars(
        query.order_by(GenerationJob.created_at.desc()).limit(limit).offset(offset)
    ).all()
    return JobPage(
        items=[job_service.job_to_out(session, j) for j in jobs],
        total=session.scalar(count_query) or 0,
        limit=limit,
        offset=offset,
    )


@router.get("/jobs/{job_id}", response_model=JobOut, tags=["jobs"], summary="Job status & progress")
def get_job(
    job: GenerationJob = Depends(get_job_or_404), session: Session = Depends(get_session)
) -> JobOut:
    return job_service.job_to_out(session, job)


@router.get(
    "/jobs/{job_id}/certificates",
    response_model=CertificatePage,
    tags=["jobs"],
    summary="Per-recipient results for a job (filterable by status)",
)
def list_job_certificates(
    job: GenerationJob = Depends(get_job_or_404),
    status_filter: CertificateStatus | None = Query(None, alias="status"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
) -> CertificatePage:
    conditions = [Certificate.job_id == job.id]
    if status_filter:
        conditions.append(Certificate.status == status_filter)
    certs = session.scalars(
        select(Certificate).where(*conditions).order_by(Certificate.row_index).limit(limit).offset(offset)
    ).all()
    total = session.scalar(select(func.count()).select_from(Certificate).where(*conditions)) or 0
    return CertificatePage(
        items=[job_service.certificate_to_out(c) for c in certs], total=total, limit=limit, offset=offset
    )


@router.get(
    "/jobs/{job_id}/download",
    tags=["jobs"],
    summary="Download all generated certificates as a ZIP (with manifest.csv)",
    response_class=FileResponse,
)
def download_job_zip(
    job: GenerationJob = Depends(get_job_or_404), session: Session = Depends(get_session)
) -> FileResponse:
    try:
        zip_path = job_service.build_job_zip(session, job)
    except job_service.JobConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"certificates-{job.id}.zip",
        background=BackgroundTask(os.remove, zip_path),  # delete temp file after sending
    )


@router.post(
    "/jobs/{job_id}/retry",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["jobs"],
    summary="Re-run generation for the job's failed certificates",
)
def retry_failed(
    job: GenerationJob = Depends(get_job_or_404),
    session: Session = Depends(get_session),
    runner: JobRunner = Depends(get_runner),
) -> JobOut:
    try:
        job_service.reset_failed_for_retry(session, job)
    except job_service.JobConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    runner.submit(job.id)
    session.expire_all()
    return job_service.job_to_out(session, session.get(GenerationJob, job.id))


# --------------------------------------------------------------------------- #
# Certificates
# --------------------------------------------------------------------------- #
@router.get(
    "/certificates/{certificate_id}",
    response_model=CertificateOut,
    tags=["certificates"],
    summary="Certificate metadata",
)
def get_certificate(cert: Certificate = Depends(get_certificate_or_404)) -> CertificateOut:
    return job_service.certificate_to_out(cert)


@router.get(
    "/certificates/{certificate_id}/download",
    tags=["certificates"],
    summary="Download a single certificate PDF",
    response_class=FileResponse,
)
def download_certificate(cert: Certificate = Depends(get_certificate_or_404)) -> FileResponse:
    if cert.status != CertificateStatus.GENERATED:
        raise HTTPException(
            status_code=409,
            detail=f"Certificate is not available (status: {cert.status.value})"
            + (f" - {cert.error_message}" if cert.error_message else ""),
        )
    if not cert.file_path or not os.path.isfile(cert.file_path):
        raise HTTPException(status_code=410, detail="Certificate file is no longer available")
    return FileResponse(
        cert.file_path,
        media_type="application/pdf",
        filename=f"{safe_filename(cert.recipient_name or '')}-{cert.verification_code}.pdf",
    )


@router.get(
    "/verify/{code}",
    response_model=VerificationOut,
    tags=["certificates"],
    summary="Public check that a certificate ID is genuine",
)
def verify_certificate(code: str, session: Session = Depends(get_session)) -> VerificationOut:
    cert = session.scalar(
        select(Certificate).where(
            Certificate.verification_code == code.strip().upper(),
            Certificate.status == CertificateStatus.GENERATED,
        )
    )
    if cert is None:
        raise HTTPException(status_code=404, detail="No certificate found with this ID")
    job = cert.job
    return VerificationOut(
        valid=True,
        recipient_name=cert.recipient_name or "",
        course_name=job.course_name,
        issuer_name=job.issuer_name,
        issue_date=job.issue_date,
        certificate_id=cert.verification_code,
    )
