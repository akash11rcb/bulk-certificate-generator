"""ORM models.

Two tables:

* ``generation_jobs``  - one row per bulk request (the shared certificate info).
* ``certificates``     - one row per recipient in that request, with its own
                         status. Invalid recipients are stored here too (status
                         ``invalid``) so a job's result is visible in one place.
"""

from __future__ import annotations

import enum
import secrets
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import JSON, Date, DateTime, Enum, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Stores datetimes in UTC and always returns timezone-aware values.

    SQLite has no timezone support, so without this the API would return
    naive timestamps (no "+00:00") - ambiguous for clients.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value


def new_id() -> str:
    return str(uuid.uuid4())


def new_verification_code() -> str:
    # 12 chars from an unambiguous alphabet, printed on the certificate.
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(12))


class JobStatus(str, enum.Enum):
    PENDING = "pending"  # accepted, waiting for a worker
    PROCESSING = "processing"  # a worker is generating certificates
    COMPLETED = "completed"  # every valid recipient got a certificate
    COMPLETED_WITH_ERRORS = "completed_with_errors"  # some generated, some failed
    FAILED = "failed"  # nothing could be generated / job-level crash


class CertificateStatus(str, enum.Enum):
    PENDING = "pending"  # valid, waiting to be generated
    GENERATED = "generated"  # PDF exists on disk
    FAILED = "failed"  # generation raised an error (retryable)
    INVALID = "invalid"  # rejected at validation time (never generated)


class GenerationJob(Base):
    __tablename__ = "generation_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, native_enum=False, length=32), default=JobStatus.PENDING, index=True
    )

    # Shared certificate information (same for every recipient in the job).
    title: Mapped[str] = mapped_column(String(120))
    course_name: Mapped[str] = mapped_column(String(200))
    issuer_name: Mapped[str] = mapped_column(String(120))
    issue_date: Mapped[date] = mapped_column(Date)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    total_recipients: Mapped[int] = mapped_column(Integer)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    certificates: Mapped[list["Certificate"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="Certificate.row_index"
    )


class Certificate(Base):
    __tablename__ = "certificates"
    __table_args__ = (Index("ix_certificates_job_status", "job_id", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("generation_jobs.id", ondelete="CASCADE"))
    # Position of the recipient in the submitted list - lets clients map
    # results (and errors) back to their own input rows.
    row_index: Mapped[int] = mapped_column(Integer)

    status: Mapped[CertificateStatus] = mapped_column(
        Enum(CertificateStatus, native_enum=False, length=32), default=CertificateStatus.PENDING
    )
    recipient_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    recipient_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    # Original payload, kept for invalid rows so the client sees what was rejected.
    raw_input: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    verification_code: Mapped[str] = mapped_column(
        String(16), unique=True, default=new_verification_code
    )
    file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    generated_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    job: Mapped[GenerationJob] = relationship(back_populates="certificates")
