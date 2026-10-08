"""Creating generation jobs + request-level validation."""

from tests.conftest import RecordingRunner, job_payload, people


def test_create_job_returns_202_with_job_details(make_client):
    client = make_client(runner_factory=RecordingRunner)
    resp = client.post("/api/v1/jobs", json=job_payload(people(3)))

    assert resp.status_code == 202
    body = resp.json()
    job = body["job"]
    assert resp.headers["Location"] == f"/api/v1/jobs/{job['id']}"
    assert body["accepted"] == 3 and body["rejected"] == 0
    assert job["status"] == "pending"
    assert job["counts"] == {"total": 3, "pending": 3, "generated": 0, "failed": 0, "invalid": 0}
    assert job["certificate"]["course_name"] == "Advanced Python"
    assert job["links"]["download_zip"].endswith("/download")
    # The job was handed to the background runner exactly once.
    assert client.app.state.runner.submitted == [job["id"]]


def test_invalid_recipients_are_skipped_and_reported_when_partial_allowed(client):
    recipients = people(2) + [
        {"name": "", "email": "x@example.com"},
        {"name": "No Email"},
    ]
    resp = client.post("/api/v1/jobs", json=job_payload(recipients))

    assert resp.status_code == 202
    body = resp.json()
    assert body["accepted"] == 2 and body["rejected"] == 2
    assert [r["row_index"] for r in body["rejected_recipients"]] == [2, 3]
    assert body["job"]["counts"]["invalid"] == 2
    assert body["job"]["counts"]["generated"] == 2
    # Invalid rows don't make the job "failed" - they were never generated.
    assert body["job"]["status"] == "completed"


def test_allow_partial_false_rejects_whole_request(client):
    recipients = people(2) + [{"name": "Bad", "email": "nope"}]
    resp = client.post("/api/v1/jobs", json=job_payload(recipients, allow_partial=False))

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "allow_partial" in detail["message"]
    assert detail["rejected_recipients"][0]["row_index"] == 2
    assert client.get("/api/v1/jobs").json()["total"] == 0  # nothing persisted


def test_request_with_no_valid_recipients_is_rejected(client):
    resp = client.post("/api/v1/jobs", json=job_payload([{"name": "", "email": "bad"}]))
    assert resp.status_code == 422
    assert resp.json()["detail"]["message"] == "No valid recipients in request."


def test_empty_recipient_list_is_rejected(client):
    assert client.post("/api/v1/jobs", json=job_payload([])).status_code == 422


def test_missing_or_blank_certificate_fields_are_rejected(client):
    payload = job_payload(people(1))
    del payload["certificate"]["course_name"]
    assert client.post("/api/v1/jobs", json=payload).status_code == 422

    payload = job_payload(people(1))
    payload["certificate"]["issuer_name"] = "   "
    assert client.post("/api/v1/jobs", json=payload).status_code == 422

    payload = job_payload(people(1))
    payload["certificate"]["issue_date"] = "not-a-date"
    assert client.post("/api/v1/jobs", json=payload).status_code == 422


def test_too_many_recipients_is_rejected(client, settings):
    resp = client.post("/api/v1/jobs", json=job_payload(people(settings.max_recipients_per_job + 1)))
    assert resp.status_code == 422
    assert "Too many recipients" in resp.json()["detail"]


def test_create_job_from_csv_upload(client):
    csv_body = (
        "﻿Name,Email,Team\n"  # BOM + mixed-case headers, as Excel exports them
        "Ada Lovelace,ada@example.com,Core\n"
        "\n"
        "Alan Turing,alan@example.com,Core\n"
        "Broken,not-an-email,Core\n"
    )
    resp = client.post(
        "/api/v1/jobs/upload",
        files={"file": ("recipients.csv", csv_body.encode(), "text/csv")},
        data={"course_name": "Data Engineering", "issuer_name": "Acme Academy"},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["accepted"] == 2 and body["rejected"] == 1
    assert body["job"]["certificate"]["course_name"] == "Data Engineering"


def test_csv_without_required_columns_is_rejected(client):
    resp = client.post(
        "/api/v1/jobs/upload",
        files={"file": ("r.csv", b"full_name,mail\nAda,ada@example.com\n", "text/csv")},
        data={"course_name": "X", "issuer_name": "Y"},
    )
    assert resp.status_code == 422
    assert "missing required column" in resp.json()["detail"]
