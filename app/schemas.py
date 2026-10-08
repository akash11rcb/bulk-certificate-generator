"""Pydantic request / response schemas.

Validation happens in two layers (see README "Validation strategy"):

1. **Request envelope** (``CreateJobRequest`` / ``CertificateInfo``) - if this is
   malformed the whole request is rejected with HTTP 422.
2. **Each recipient** (``RecipientIn``) - validated one by one in
   ``app.services.validation`` so a single bad row does not sink the batch.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models import CertificateStatus, JobStatus

_WHITESPACE = re.compile(r"\s+")


def _clean_text(value: str) -> str:
    """Normalise unicode, collapse whitespace and reject control characters."""
    value = unicodedata.normalize("NFC", value)
    if any(unicodedata.category(ch) == "Cc" for ch in value if ch not in "\t\n\r"):
        raise ValueError("must not contain control characters")
    return _WHITESPACE.sub(" ", value).strip()


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #
class CertificateInfo(BaseModel):
    """Information shared by every certificate in a job."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="Certificate of Completion", min_length=1, max_length=120)
    course_name: str = Field(min_length=1, max_length=200, examples=["Advanced Python"])
    issuer_name: str = Field(min_length=1, max_length=120, examples=["Acme Academy"])
    issue_date: date = Field(default_factory=date.today, examples=["2026-10-08"])
    description: str | None = Field(default=None, max_length=300)

    @field_validator("title", "course_name", "issuer_name", "description")
    @classmethod
    def _clean(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = _clean_text(v)
        if not v:
            raise ValueError("must not be blank")
        return v


class RecipientIn(BaseModel):
    """A single recipient. Unknown extra keys are ignored."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=100)
    email: EmailStr

    @field_validator("name", mode="before")
    @classmethod
    def _clean_name(cls, v: Any) -> Any:
        if not isinstance(v, str):
            return v  # let pydantic report the type error
        v = _clean_text(v)
        if not v:
            raise ValueError("must not be blank")
        if not any(ch.isalpha() for ch in v):
            raise ValueError("must contain at least one letter")
        return v

    @field_validator("email", mode="after")
    @classmethod
    def _lower_email(cls, v: str) -> str:
        return v.lower()


class CreateJobRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "certificate": {
                    "title": "Certificate of Completion",
                    "course_name": "Advanced Python",
                    "issuer_name": "Acme Academy",
                    "issue_date": "2026-10-08",
                },
                "recipients": [
                    {"name": "Ada Lovelace", "email": "ada@example.com"},
                    {"name": "Alan Turing", "email": "alan@example.com"},
                ],
                "allow_partial": True,
            }
        },
    )

    certificate: CertificateInfo
    # Typed loosely on purpose: each item is validated individually so that
    # one bad row is reported instead of failing the entire request.
    recipients: list[Any] = Field(min_length=1)
    allow_partial: bool = Field(
        default=True,
        description=(
            "If true, invalid recipients are skipped (and reported) while valid ones are "
            "generated. If false, any invalid recipient rejects the whole request with 422."
        ),
    )


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #
class RecipientError(BaseModel):
    row_index: int
    input: Any
    errors: list[str]


class JobCounts(BaseModel):
    total: int
    pending: int
    generated: int
    failed: int
    invalid: int


class JobOut(BaseModel):
    id: str
    status: JobStatus
    certificate: CertificateInfo
    counts: JobCounts
    progress_percent: float = Field(
        description="Share of valid recipients that have finished (generated or failed)."
    )
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    links: dict[str, str]


class JobCreatedOut(BaseModel):
    job: JobOut
    accepted: int
    rejected: int
    rejected_recipients: list[RecipientError]


class CertificateOut(BaseModel):
    id: str
    job_id: str
    row_index: int
    status: CertificateStatus
    recipient_name: str | None
    recipient_email: str | None
    verification_code: str | None
    error_message: str | None
    attempts: int
    generated_at: datetime | None
    download_url: str | None
    raw_input: Any | None = None


class CertificatePage(BaseModel):
    items: list[CertificateOut]
    total: int
    limit: int
    offset: int


class JobPage(BaseModel):
    items: list[JobOut]
    total: int
    limit: int
    offset: int


class VerificationOut(BaseModel):
    valid: bool
    recipient_name: str
    course_name: str
    issuer_name: str
    issue_date: date
    certificate_id: str
