"""FastAPI dependencies - pull shared services off ``app.state``."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Certificate, GenerationJob
from app.services.runner import JobRunner


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session


def get_runner(request: Request) -> JobRunner:
    return request.app.state.runner


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_job_or_404(job_id: str, session: Session = Depends(get_session)) -> GenerationJob:
    job = session.get(GenerationJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return job


def get_certificate_or_404(
    certificate_id: str, session: Session = Depends(get_session)
) -> Certificate:
    cert = session.get(Certificate, certificate_id)
    if cert is None:
        raise HTTPException(status_code=404, detail=f"Certificate '{certificate_id}' not found")
    return cert
