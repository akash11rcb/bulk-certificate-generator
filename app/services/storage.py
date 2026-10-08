"""File storage for generated certificates (local disk).

Kept behind a tiny class so it can be swapped for S3/GCS without touching the
processing code: only ``path_for`` would need a new implementation
(plus returning pre-signed URLs from the download endpoint).
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path


def safe_filename(name: str, fallback: str = "certificate") -> str:
    """ASCII-only, filesystem-safe slug, e.g. 'Đặng Ñúñez' -> 'dang-nunez'."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^A-Za-z0-9]+", "-", ascii_name).strip("-").lower()
    return slug[:60] or fallback


class LocalStorage:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)  # sub-folders are created on first write

    def path_for(self, job_id: str, certificate_id: str) -> Path:
        # Files are named by IDs (never user input) to avoid path traversal.
        return self.root / job_id / f"{certificate_id}.pdf"
