"""End-to-end demo client (standard library only).

Submits examples/request.json, polls the job until it finishes, prints the
per-recipient results and downloads the ZIP of certificates.

    python scripts/demo.py                       # against http://localhost:8000
    python scripts/demo.py --base-url http://host:8000 --request my.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def call(method: str, url: str, body: dict | None = None) -> tuple[int, bytes]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--request", default=str(ROOT / "examples" / "request.json"))
    parser.add_argument("--out", default=str(ROOT / "downloads"))
    args = parser.parse_args()
    api = args.base_url.rstrip("/") + "/api/v1"

    payload = json.loads(Path(args.request).read_text(encoding="utf-8"))
    status, body = call("POST", f"{api}/jobs", payload)
    if status != 202:
        print(f"Request rejected ({status}):\n{body.decode()}")
        return 1
    created = json.loads(body)
    job_id = created["job"]["id"]
    print(f"Job {job_id} accepted: {created['accepted']} valid, {created['rejected']} rejected")
    for r in created["rejected_recipients"]:
        print(f"  ✗ row {r['row_index']}: {'; '.join(r['errors'])}")

    while True:
        _, body = call("GET", f"{api}/jobs/{job_id}")
        job = json.loads(body)
        print(f"  status={job['status']:<22} progress={job['progress_percent']:>6}%  {job['counts']}")
        if job["status"] not in ("pending", "processing"):
            break
        time.sleep(0.5)

    _, body = call("GET", f"{api}/jobs/{job_id}/certificates?limit=1000")
    print("\nPer-recipient results:")
    for c in json.loads(body)["items"]:
        detail = c["download_url"] or c["error_message"]
        print(f"  [{c['row_index']}] {c['status']:<9} {c['recipient_name'] or '-':<22} {detail}")

    status, content = call("GET", f"{api}/jobs/{job_id}/download")
    if status == 200:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        zip_path = out / f"certificates-{job_id}.zip"
        zip_path.write_bytes(content)
        print(f"\nDownloaded {zip_path} ({len(content) // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
