"""Certificate rendering (the predefined template) and end-to-end generation."""

from datetime import date
from pathlib import Path

import pytest
from pypdf import PdfReader

from app.services.renderer import (
    CertificateData,
    UnsupportedCharactersError,
    render_certificate,
)
from tests.conftest import job_payload


def _data(**overrides) -> CertificateData:
    base = dict(
        recipient_name="Ada Lovelace",
        title="Certificate of Completion",
        course_name="Advanced Python",
        issuer_name="Acme Academy",
        issue_date=date(2026, 10, 8),
        verification_code="ABCDEFGH2345",
        verify_url="http://testserver/api/v1/verify/ABCDEFGH2345",
        description="For outstanding work.",
    )
    base.update(overrides)
    return CertificateData(**base)


def _pdf_text(path: Path) -> str:
    return " ".join(page.extract_text() for page in PdfReader(str(path)).pages)


def test_renders_single_page_pdf_with_recipient_specific_content(tmp_path):
    out = render_certificate(_data(), tmp_path / "cert.pdf")

    reader = PdfReader(str(out))
    assert len(reader.pages) == 1
    width, height = reader.pages[0].mediabox.width, reader.pages[0].mediabox.height
    assert width > height  # landscape template
    text = _pdf_text(out)
    for expected in ("Ada Lovelace", "Advanced Python", "Acme Academy", "08 October 2026", "ABCDEFGH2345"):
        assert expected in text
    assert reader.metadata.title == "Certificate of Completion - Ada Lovelace"


def test_renders_accented_and_cyrillic_names(tmp_path):
    out = render_certificate(_data(recipient_name="Zoë Ñúñez-Иванова"), tmp_path / "c.pdf")
    assert "Zoë Ñúñez-Иванова" in _pdf_text(out)


def test_very_long_name_still_renders(tmp_path):
    out = render_certificate(_data(recipient_name="Wolfeschlegelsteinhausen " * 4), tmp_path / "c.pdf")
    assert out.exists() and out.stat().st_size > 0


def test_unsupported_characters_raise_clear_error_and_leave_no_file(tmp_path):
    target = tmp_path / "c.pdf"
    with pytest.raises(UnsupportedCharactersError, match="recipient name"):
        render_certificate(_data(recipient_name="李雷"), target)
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []  # no temp files left behind


def test_job_generates_one_pdf_per_valid_recipient(client, settings):
    recipients = [
        {"name": "Ada Lovelace", "email": "ada@example.com"},
        {"name": "Alan Turing", "email": "alan@example.com"},
    ]
    job = client.post("/api/v1/jobs", json=job_payload(recipients)).json()["job"]
    assert job["status"] == "completed"

    certs = client.get(f"/api/v1/jobs/{job['id']}/certificates").json()["items"]
    assert [c["status"] for c in certs] == ["generated", "generated"]
    files = sorted((settings.storage_dir / job["id"]).glob("*.pdf"))
    assert len(files) == 2
    names = {
        _pdf_text(f).split("This is to certify that")[1].split("has successfully")[0].strip()
        for f in files
    }
    assert names == {"Ada Lovelace", "Alan Turing"}
    # Each certificate gets a unique verification code.
    assert len({c["verification_code"] for c in certs}) == 2
