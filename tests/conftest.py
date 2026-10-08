"""Shared fixtures.

Every test gets its own temporary SQLite database and storage folder. By
default jobs run *inline* (synchronously) so tests can assert on the final
state straight after the POST, without sleeping or polling.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.renderer import CertificateData, RenderError, render_certificate
from app.services.runner import InlineJobRunner


class RecordingRunner:
    """Captures submitted job IDs without running them - lets tests observe
    the 'pending' state and then drive processing step by step."""

    def __init__(self, process: Callable[[str], None]) -> None:
        self.process = process
        self.submitted: list[str] = []

    def submit(self, job_id: str) -> None:
        self.submitted.append(job_id)

    def run_all(self) -> None:
        while self.submitted:
            self.process(self.submitted.pop(0))

    def shutdown(self) -> None:
        pass


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        storage_dir=tmp_path / "certificates",
        max_recipients_per_job=50,
        worker_threads=2,
        public_base_url="http://testserver",
    )


@pytest.fixture
def make_client(settings: Settings):
    """Factory: build a TestClient with an optional custom runner / renderer."""
    clients: list[TestClient] = []

    def _make(runner_factory=InlineJobRunner, renderer=render_certificate) -> TestClient:
        app = create_app(settings, runner_factory=runner_factory, renderer=renderer)
        client = TestClient(app)
        client.__enter__()  # run lifespan (creates tables)
        clients.append(client)
        return client

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()


def failing_renderer(fail_for: set[str]):
    """A renderer that raises for specific recipient names and works for everyone else."""

    def _render(data: CertificateData, path: Path) -> Path:
        if data.recipient_name in fail_for:
            raise RenderError(f"simulated failure for {data.recipient_name}")
        return render_certificate(data, path)

    return _render


def job_payload(recipients: list, **overrides) -> dict:
    payload = {
        "certificate": {
            "title": "Certificate of Completion",
            "course_name": "Advanced Python",
            "issuer_name": "Acme Academy",
            "issue_date": "2026-10-08",
        },
        "recipients": recipients,
    }
    payload.update(overrides)
    return payload


def people(n: int) -> list[dict]:
    return [{"name": f"Person {i}", "email": f"person{i}@example.com"} for i in range(n)]
