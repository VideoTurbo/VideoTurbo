from __future__ import annotations

import hashlib
import secrets
from pathlib import Path


def new_worker_token() -> str:
    return secrets.token_urlsafe(32)


def secure_equals(left: str, right: str) -> bool:
    return secrets.compare_digest(left.encode(), right.encode())


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()
