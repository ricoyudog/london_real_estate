"""Immutable content-addressed artifact storage.

The store persists bytes before any higher-level code records database
metadata.  It does not itself write evidence rows, so a caller can keep the
file-first/DB-second transaction boundary explicit.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import os
import stat
from collections.abc import Callable
from typing import BinaryIO
from uuid import uuid4


DEFAULT_CHUNK_SIZE = 1024 * 1024


class ArtifactError(RuntimeError):
    """Base class for immutable artifact-store failures."""


class ArtifactTooLargeError(ArtifactError):
    """A streamed artifact exceeded the caller's declared byte limit."""


class ArtifactVerificationError(ArtifactError):
    """A requested CAS object is absent, unsafe, or does not match its digest."""


@dataclass(frozen=True)
class StoredArtifact:
    """A verified immutable object in the local content-addressed store."""

    content_sha256: str
    byte_size: int
    media_type: str | None
    path: Path
    created: bool

    @property
    def artifact_uri(self) -> str:
        """Return the stable logical URI stored in database metadata."""

        return f"evidence/sha256/{self.content_sha256[:2]}/{self.content_sha256}"


class ArtifactStore:
    """Store immutable bytes at ``<data-dir>/evidence/sha256/<prefix>/<hash>``."""

    def __init__(self, data_directory: str | Path) -> None:
        self.data_directory = Path(data_directory).expanduser().resolve()
        self._objects_directory = self.data_directory / "evidence" / "sha256"
        self._temporary_directory = self.data_directory / "evidence" / ".tmp"

    def put_bytes(
        self,
        content: bytes,
        *,
        media_type: str | None = None,
        max_bytes: int | None = None,
    ) -> StoredArtifact:
        """Store an in-memory payload through the same streaming code path."""

        from io import BytesIO

        return self.put_stream(BytesIO(content), media_type=media_type, max_bytes=max_bytes)

    def put_stream(
        self,
        stream: BinaryIO,
        *,
        media_type: str | None = None,
        max_bytes: int | None = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        validator: Callable[[Path, int], None] | None = None,
        before_publish: Callable[[], None] | None = None,
    ) -> StoredArtifact:
        """Stream bytes to a private temp file, fsync, and atomically publish it.

        Existing objects are never replaced.  A concurrent writer that wins
        publication causes this writer to verify and reuse the object instead.
        An optional validator runs against the private, fsynced temporary file
        before it is linked into the published CAS namespace.
        """

        if max_bytes is not None and max_bytes < 0:
            raise ValueError("max_bytes must be non-negative")
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        self._ensure_directories()
        temporary_path = self._temporary_directory / uuid4().hex
        descriptor = self._open_temporary(temporary_path)
        hasher = sha256()
        byte_size = 0
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as output:
                while True:
                    chunk = stream.read(chunk_size)
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        raise TypeError("artifact streams must yield bytes")
                    byte_size += len(chunk)
                    if max_bytes is not None and byte_size > max_bytes:
                        raise ArtifactTooLargeError(
                            f"artifact exceeds maximum size of {max_bytes} bytes"
                        )
                    hasher.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())

            if validator is not None:
                validator(temporary_path, byte_size)
            if before_publish is not None:
                before_publish()
            content_sha256 = hasher.hexdigest()
            destination = self.object_path(content_sha256)
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                os.link(temporary_path, destination)
            except FileExistsError:
                temporary_path.unlink(missing_ok=True)
                artifact = StoredArtifact(
                    content_sha256=content_sha256,
                    byte_size=byte_size,
                    media_type=media_type,
                    path=destination,
                    created=False,
                )
                self._require_verified(artifact)
                return artifact

            temporary_path.unlink()
            _fsync_directory(destination.parent)
            return StoredArtifact(
                content_sha256=content_sha256,
                byte_size=byte_size,
                media_type=media_type,
                path=destination,
                created=True,
            )
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise

    def object_path(self, content_sha256: str) -> Path:
        """Return the physical path for a validated SHA-256 digest."""

        _validate_digest(content_sha256)
        return self._objects_directory / content_sha256[:2] / content_sha256

    def verify(self, artifact: StoredArtifact | str) -> bool:
        """Return whether an object exists as a regular file with the right hash."""

        content_sha256, expected_size = _artifact_identity(artifact)
        path = self.object_path(content_sha256)
        try:
            file_stat = path.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(file_stat.st_mode):
            return False
        if expected_size is not None and file_stat.st_size != expected_size:
            return False
        return _hash_file(path) == content_sha256

    def open(self, artifact: StoredArtifact | str) -> BinaryIO:
        """Open a verified object for read-only parser consumption."""

        content_sha256, _ = _artifact_identity(artifact)
        path = self.object_path(content_sha256)
        if not self.verify(artifact):
            raise ArtifactVerificationError(f"artifact failed verification: {content_sha256}")
        return path.open("rb")

    def published_digests(self) -> tuple[str, ...]:
        """List canonical CAS object names without touching private temp files."""

        if not self._objects_directory.is_dir():
            return ()
        digests: list[str] = []
        for prefix in sorted(self._objects_directory.iterdir()):
            if not prefix.is_dir():
                continue
            for candidate in sorted(prefix.iterdir()):
                digest = candidate.name
                try:
                    _validate_digest(digest)
                except ValueError:
                    continue
                if prefix.name == digest[:2]:
                    digests.append(digest)
        return tuple(digests)

    def _require_verified(self, artifact: StoredArtifact) -> None:
        if not self.verify(artifact):
            raise ArtifactVerificationError(
                f"existing object failed verification: {artifact.content_sha256}"
            )

    def _ensure_directories(self) -> None:
        self._objects_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._temporary_directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    @staticmethod
    def _open_temporary(path: Path) -> int:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        return os.open(path, flags, 0o600)


def _artifact_identity(artifact: StoredArtifact | str) -> tuple[str, int | None]:
    if isinstance(artifact, StoredArtifact):
        return artifact.content_sha256, artifact.byte_size
    _validate_digest(artifact)
    return artifact, None


def _validate_digest(content_sha256: str) -> None:
    if len(content_sha256) != 64 or any(character not in "0123456789abcdef" for character in content_sha256):
        raise ValueError("content_sha256 must be a lowercase SHA-256 hexadecimal digest")


def _hash_file(path: Path) -> str:
    hasher = sha256()
    with path.open("rb") as artifact:
        while chunk := artifact.read(DEFAULT_CHUNK_SIZE):
            hasher.update(chunk)
    return hasher.hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
