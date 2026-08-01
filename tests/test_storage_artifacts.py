from __future__ import annotations

from hashlib import sha256
from io import BytesIO
from pathlib import Path

import pytest

from nan_fung.storage.artifacts import (
    ArtifactStore,
    ArtifactTooLargeError,
    ArtifactVerificationError,
)


def test_artifacts_are_written_to_the_content_addressed_layout(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    payload = b"immutable evidence"

    artifact = store.put_stream(BytesIO(payload), media_type="text/plain")

    expected_digest = sha256(payload).hexdigest()
    assert artifact.content_sha256 == expected_digest
    assert artifact.byte_size == len(payload)
    assert artifact.media_type == "text/plain"
    assert artifact.created
    assert artifact.path == tmp_path / "evidence" / "sha256" / expected_digest[:2] / expected_digest
    assert artifact.artifact_uri == f"evidence/sha256/{expected_digest[:2]}/{expected_digest}"
    assert artifact.path.read_bytes() == payload
    assert artifact.path.stat().st_mode & 0o777 == 0o600
    assert store.verify(artifact)


def test_duplicate_content_reuses_the_existing_immutable_object(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)

    first = store.put_bytes(b"same bytes")
    second = store.put_bytes(b"same bytes")

    assert first.created
    assert not second.created
    assert second.path == first.path
    assert store.verify(second.content_sha256)


def test_verification_detects_tampered_or_missing_content(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    artifact = store.put_bytes(b"before")

    artifact.path.write_bytes(b"after")

    assert not store.verify(artifact)
    with pytest.raises(ArtifactVerificationError, match="failed verification"):
        store.open(artifact)


def test_stream_limit_cleans_up_the_unpublished_temp_file(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)

    with pytest.raises(ArtifactTooLargeError):
        store.put_stream(BytesIO(b"larger than limit"), max_bytes=4)

    temporary_directory = tmp_path / "evidence" / ".tmp"
    assert list(temporary_directory.iterdir()) == []
    assert list((tmp_path / "evidence" / "sha256").iterdir()) == []


def test_duplicate_write_refuses_to_replace_a_corrupt_existing_object(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    artifact = store.put_bytes(b"original")
    artifact.path.write_bytes(b"tampered")

    with pytest.raises(ArtifactVerificationError, match="existing object"):
        store.put_bytes(b"original")

    assert artifact.path.read_bytes() == b"tampered"


def test_digest_paths_reject_traversal_and_noncanonical_values(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)

    with pytest.raises(ValueError, match="lowercase SHA-256"):
        store.object_path("../not-a-digest")
