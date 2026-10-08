"""A failing certificate must not stop the rest of the job."""

from app.services.renderer import render_certificate
from tests.conftest import RecordingRunner, failing_renderer, job_payload, people


def test_one_failure_does_not_block_other_certificates(make_client, settings):
    client = make_client(renderer=failing_renderer({"Person 2"}))
    body = client.post("/api/v1/jobs", json=job_payload(people(5))).json()
    job = body["job"]

    assert job["status"] == "completed_with_errors"
    assert job["counts"]["generated"] == 4
    assert job["counts"]["failed"] == 1
    assert job["progress_percent"] == 100
    # Certificates after the failing one were still generated.
    assert len(list((settings.storage_dir / job["id"]).glob("*.pdf"))) == 4


def test_unexpected_exception_is_isolated_to_its_certificate(make_client):
    def crashing_renderer(data, path):
        if data.recipient_name == "Person 0":
            raise MemoryError("boom")  # not a RenderError - totally unexpected
        return render_certificate(data, path)

    client = make_client(renderer=crashing_renderer)
    job = client.post("/api/v1/jobs", json=job_payload(people(3))).json()["job"]
    assert job["counts"]["generated"] == 2

    failed = client.get(
        f"/api/v1/jobs/{job['id']}/certificates", params={"status": "failed"}
    ).json()["items"]
    assert failed[0]["error_message"] == "MemoryError: boom"


def test_real_render_failure_for_unsupported_characters(client):
    recipients = [
        {"name": "Ada Lovelace", "email": "ada@example.com"},
        {"name": "李雷", "email": "li@example.com"},  # valid input, but the template font can't draw it
    ]
    job = client.post("/api/v1/jobs", json=job_payload(recipients)).json()["job"]
    assert job["status"] == "completed_with_errors"
    failed = client.get(f"/api/v1/jobs/{job['id']}/certificates?status=failed").json()["items"]
    assert "cannot render" in failed[0]["error_message"]


def test_job_fails_when_every_certificate_fails(make_client):
    client = make_client(renderer=failing_renderer({"Person 0", "Person 1"}))
    job = client.post("/api/v1/jobs", json=job_payload(people(2))).json()["job"]
    assert job["status"] == "failed"
    assert job["counts"]["failed"] == 2


def test_retry_regenerates_only_failed_certificates(make_client, settings):
    broken = {"Person 1"}
    client = make_client(renderer=failing_renderer(broken))
    job_id = client.post("/api/v1/jobs", json=job_payload(people(3))).json()["job"]["id"]
    assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "completed_with_errors"

    broken.clear()  # the transient problem is "fixed"
    resp = client.post(f"/api/v1/jobs/{job_id}/retry")
    assert resp.status_code == 202

    job = client.get(f"/api/v1/jobs/{job_id}").json()
    assert job["status"] == "completed"
    assert job["counts"]["generated"] == 3 and job["counts"]["failed"] == 0
    certs = client.get(f"/api/v1/jobs/{job_id}/certificates").json()["items"]
    attempts = {c["row_index"]: c["attempts"] for c in certs}
    assert attempts == {0: 1, 1: 2, 2: 1}  # only the failed one was re-rendered


def test_retry_conflicts(make_client):
    client = make_client(runner_factory=RecordingRunner)
    job_id = client.post("/api/v1/jobs", json=job_payload(people(1))).json()["job"]["id"]
    # Still pending -> cannot retry.
    assert client.post(f"/api/v1/jobs/{job_id}/retry").status_code == 409
    client.app.state.runner.run_all()
    # Completed with no failures -> nothing to retry.
    resp = client.post(f"/api/v1/jobs/{job_id}/retry")
    assert resp.status_code == 409
    assert "no failed certificates" in resp.json()["detail"]


def test_job_level_crash_marks_job_failed(make_client, monkeypatch):
    client = make_client(runner_factory=RecordingRunner)
    job_id = client.post("/api/v1/jobs", json=job_payload(people(2))).json()["job"]["id"]

    processor = client.app.state.processor

    def explode(*args, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(processor, "_run", explode)
    client.app.state.runner.run_all()

    job = client.get(f"/api/v1/jobs/{job_id}").json()
    assert job["status"] == "failed"
    assert "database went away" in job["error_message"]
