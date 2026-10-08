"""Job status and progress tracking."""

import time

from app.services.runner import ThreadPoolJobRunner
from tests.conftest import RecordingRunner, failing_renderer, job_payload, people


def test_status_moves_from_pending_to_completed_with_progress(make_client):
    client = make_client(runner_factory=RecordingRunner)
    job_id = client.post("/api/v1/jobs", json=job_payload(people(4))).json()["job"]["id"]

    before = client.get(f"/api/v1/jobs/{job_id}").json()
    assert before["status"] == "pending"
    assert before["progress_percent"] == 0
    assert before["started_at"] is None

    client.app.state.runner.run_all()

    after = client.get(f"/api/v1/jobs/{job_id}").json()
    assert after["status"] == "completed"
    assert after["progress_percent"] == 100
    assert after["counts"]["generated"] == 4 and after["counts"]["pending"] == 0
    assert after["started_at"] and after["completed_at"]


def test_progress_is_visible_while_job_is_running(make_client):
    """Progress is committed per certificate, so a poll mid-job sees partial counts."""
    snapshots = []

    def observing_renderer(data, path):
        # Called for each certificate, before it is marked generated.
        snapshots.append(client.get(f"/api/v1/jobs/{job_id}").json())
        from app.services.renderer import render_certificate

        return render_certificate(data, path)

    client = make_client(runner_factory=RecordingRunner, renderer=observing_renderer)
    job_id = client.post("/api/v1/jobs", json=job_payload(people(4))).json()["job"]["id"]
    client.app.state.runner.run_all()

    assert [s["status"] for s in snapshots] == ["processing"] * 4
    assert [s["counts"]["generated"] for s in snapshots] == [0, 1, 2, 3]
    assert [s["progress_percent"] for s in snapshots] == [0, 25, 50, 75]


def test_progress_ignores_invalid_rows(make_client):
    client = make_client(runner_factory=RecordingRunner)
    recipients = people(2) + [{"name": "", "email": "bad"}, {"name": "x", "email": "bad"}]
    job_id = client.post("/api/v1/jobs", json=job_payload(recipients)).json()["job"]["id"]
    client.app.state.runner.run_all()

    job = client.get(f"/api/v1/jobs/{job_id}").json()
    assert job["progress_percent"] == 100  # 2 of 2 *processable* rows done
    assert job["counts"] == {"total": 4, "pending": 0, "generated": 2, "failed": 0, "invalid": 2}


def test_status_identifies_successful_and_failed_certificates(make_client):
    client = make_client(renderer=failing_renderer({"Person 1"}))
    job_id = client.post("/api/v1/jobs", json=job_payload(people(3))).json()["job"]["id"]

    failed = client.get(f"/api/v1/jobs/{job_id}/certificates", params={"status": "failed"}).json()
    generated = client.get(f"/api/v1/jobs/{job_id}/certificates", params={"status": "generated"}).json()
    assert [c["row_index"] for c in failed["items"]] == [1]
    assert failed["items"][0]["error_message"] == "simulated failure for Person 1"
    assert failed["items"][0]["download_url"] is None
    assert [c["row_index"] for c in generated["items"]] == [0, 2]


def test_certificates_listing_is_paginated(client):
    job_id = client.post("/api/v1/jobs", json=job_payload(people(5))).json()["job"]["id"]
    page = client.get(f"/api/v1/jobs/{job_id}/certificates", params={"limit": 2, "offset": 2}).json()
    assert page["total"] == 5
    assert [c["row_index"] for c in page["items"]] == [2, 3]


def test_list_jobs_and_unknown_job(client):
    client.post("/api/v1/jobs", json=job_payload(people(1)))
    client.post("/api/v1/jobs", json=job_payload(people(2)))
    listing = client.get("/api/v1/jobs").json()
    assert listing["total"] == 2
    assert client.get("/api/v1/jobs", params={"status": "completed"}).json()["total"] == 2
    assert client.get("/api/v1/jobs/does-not-exist").status_code == 404


def test_background_thread_pool_processes_job(make_client):
    """Real background execution: the POST returns before generation finishes."""
    client = make_client(runner_factory=lambda p: ThreadPoolJobRunner(p, max_workers=2))
    job_id = client.post("/api/v1/jobs", json=job_payload(people(5))).json()["job"]["id"]

    deadline = time.time() + 20
    while time.time() < deadline:
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["status"] not in ("pending", "processing"):
            break
        time.sleep(0.05)
    assert job["status"] == "completed"
    assert job["counts"]["generated"] == 5


def test_job_cannot_be_processed_twice(make_client):
    client = make_client(runner_factory=RecordingRunner)
    job_id = client.post("/api/v1/jobs", json=job_payload(people(2))).json()["job"]["id"]
    processor = client.app.state.processor
    processor.process(job_id)
    processor.process(job_id)  # second call finds the job no longer 'pending' and skips

    certs = client.get(f"/api/v1/jobs/{job_id}/certificates").json()["items"]
    assert [c["attempts"] for c in certs] == [1, 1]


def test_unfinished_jobs_resume_on_startup(make_client):
    first = make_client(runner_factory=RecordingRunner)  # accepts but never runs the job
    job_id = first.post("/api/v1/jobs", json=job_payload(people(3))).json()["job"]["id"]
    assert first.get(f"/api/v1/jobs/{job_id}").json()["status"] == "pending"

    # "Restart" the service against the same database.
    second = make_client()
    job = second.get(f"/api/v1/jobs/{job_id}").json()
    assert job["status"] == "completed"
    assert job["counts"]["generated"] == 3
