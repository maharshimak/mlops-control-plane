"""Artifact identity: SHA-256 of artifact bytes and confined local verification."""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from pathlib import Path

SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_CHUNK = 1024 * 1024


class ArtifactAccessError(ValueError):
    """The requested artifact path is not allowed or not readable."""


@dataclass(frozen=True, slots=True)
class ArtifactDigest:
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ArtifactVerification:
    verified: bool
    expected_sha256: str
    actual_sha256: str
    size_bytes: int
    reason: str | None


def is_sha256(value: object) -> bool:
    return isinstance(value, str) and SHA256_PATTERN.fullmatch(value) is not None


def digest_bytes(data: bytes) -> ArtifactDigest:
    return ArtifactDigest(hashlib.sha256(data).hexdigest(), len(data))


def digest_file(path: Path) -> ArtifactDigest:
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            hasher.update(chunk)
            size += len(chunk)
    return ArtifactDigest(hasher.hexdigest(), size)


def resolve_confined(root: str | Path, relative_path: str) -> Path:
    """Resolve ``relative_path`` under ``root`` and refuse anything that escapes it.

    Absolute paths, ``..`` traversal and symlinks pointing outside the root are
    rejected because the resolved real path must stay inside the resolved root.
    """
    if not relative_path or "\x00" in relative_path:
        raise ArtifactAccessError("artifact path is required")
    candidate = Path(relative_path)
    if candidate.is_absolute() or candidate.drive or candidate.anchor:
        raise ArtifactAccessError("artifact path must be relative to the artifact root")
    if ".." in candidate.parts:
        raise ArtifactAccessError("artifact path cannot contain parent traversal")
    base = Path(root).expanduser().resolve(strict=True)
    resolved = (base / candidate).resolve(strict=False)
    if not resolved.is_relative_to(base):
        raise ArtifactAccessError("artifact path escapes the artifact root")
    if not resolved.is_file():
        raise ArtifactAccessError("artifact file not found")
    return resolved


def compare_digest(expected_sha256: str, actual: ArtifactDigest) -> ArtifactVerification:
    if not is_sha256(expected_sha256):
        raise ValueError("expected_sha256 must be 64 lowercase hex characters")
    verified = hmac.compare_digest(expected_sha256, actual.sha256)
    return ArtifactVerification(
        verified=verified,
        expected_sha256=expected_sha256,
        actual_sha256=actual.sha256,
        size_bytes=actual.size_bytes,
        reason=None if verified else "artifact SHA-256 does not match the registered digest",
    )
