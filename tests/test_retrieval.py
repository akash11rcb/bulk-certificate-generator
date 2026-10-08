"""Retrieving generated certificates (single PDF, ZIP, metadata, verification)."""

import csv
import io
import zipfile

from tests.conftest import RecordingRunner, failing_renderer, job_payload, people


def _generated(client, job_id):
    return client.get(f"/api/v1/jobs/{job_id}/certificates", params={"status": "generated"}).json()["items"]


def test_download_single_certificate_pdf(client):
    job_id = client.post(
        "/api/v1/jobs", json=job_payload([{"name": "Zoë Ñúñez", "email": "zoe@example.com"}])
    ).json()["job"]["id"]
    cert = _generated(client, job_id)[0]

    resp = client.get(cert["download_url"])
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content.startswith(b"%PDF")
    disposition = resp.headers["content-disposition"]
    assert "attachment" in disposition and "zoe-nunez-" in disposition


def test_certificate_metadata(client):
    job_id = client.post("/api/v1/jobs", json=job_payload(people(1))).json()["job"]["id"]
    cert = _generated(client, job_id)[0]
    meta = client.get(f"/api/v1/certificates/{cert['id']}").json()
    assert meta["recipient_email"] == "person0@example.com"
    assert meta["status"] == "generated"
    assert meta["download_url"] == f"/api/v1/certificates/{cert['id']}/download"


def test_cannot_download_certificate_that_is_not_generated(make_client):
    client = make_client(runner_factory=RecordingRunner)
    job_id = client.post("/api/v1/jobs", json=job_payload(people(1))).json()["job"]["id"]
    cert = client.get(f"/api/v1/jobs/{job_id}/certificates").json()["items"][0]

    resp = client.get(f"/api/v1/certificates/{cert['id']}/download")
    assert resp.status_code == 409
    assert "pending" in resp.json()["detail"]


def test_failed_certificate_download_explains_why(make_client):
    client = make_client(renderer=failing_renderer({"Person 0"}))
    job_id = client.post("/api/v1/jobs", json=job_payload(people(2))).json()["job"]["id"]
    failed = client.get(f"/api/v1/jobs/{job_id}/certificates?status=failed").json()["items"][0]
    resp = client.get(f"/api/v1/certificates/{failed['id']}/download")
    assert resp.status_code == 409
    assert "simulated failure" in resp.json()["detail"]


def test_unknown_certificate_returns_404(client):
    assert client.get("/api/v1/certificates/nope").status_code == 404
    assert client.get("/api/v1/certificates/nope/download").status_code == 404


def test_download_job_zip_contains_pdfs_and_manifest(make_client):
    client = make_client(renderer=failing_renderer({"Person 1"}))
    recipients = people(3) + [{"name": "", "email": "bad"}]
    job_id = client.post("/api/v1/jobs", json=job_payload(recipients)).json()["job"]["id"]

    resp = client.get(f"/api/v1/jobs/{job_id}/download")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"

    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    pdfs = sorted(n for n in zf.namelist() if n.endswith(".pdf"))
    assert pdfs == ["certificates/00000_person-0.pdf", "certificates/00002_person-2.pdf"]
    assert zf.read(pdfs[0]).startswith(b"%PDF")

    manifest = list(csv.DictReader(io.StringIO(zf.read("manifest.csv").decode())))
    assert [(r["row_index"], r["status"]) for r in manifest] == [
        ("0", "generated"), ("1", "failed"), ("2", "generated"), ("3", "invalid"),
    ]
    assert manifest[1]["error"] == "simulated failure for Person 1"


def test_zip_download_before_anything_is_generated_returns_409(make_client):
    client = make_client(runner_factory=RecordingRunner)
    job_id = client.post("/api/v1/jobs", json=job_payload(people(1))).json()["job"]["id"]
    assert client.get(f"/api/v1/jobs/{job_id}/download").status_code == 409


def test_public_verification_endpoint(client):
    job_id = client.post("/api/v1/jobs", json=job_payload(people(1))).json()["job"]["id"]
    code = _generated(client, job_id)[0]["verification_code"]

    resp = client.get(f"/api/v1/verify/{code.lower()}")  # case-insensitive
    assert resp.status_code == 200
    assert resp.json()["recipient_name"] == "Person 0"
    assert resp.json()["course_name"] == "Advanced Python"
    assert client.get("/api/v1/verify/NOTAREALCODE").status_code == 404
