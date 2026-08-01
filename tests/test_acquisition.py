from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nan_fung.datasources import common
from nan_fung.datasources.common import HostThrottleBlocked
from nan_fung.ingestion.policies import ArtifactPolicy, PolicyError, SourcePolicy
from nan_fung.storage.artifacts import ArtifactStore, ArtifactTooLargeError


def _policy(*, query_keys: tuple[str, ...] = ()) -> SourcePolicy:
    return SourcePolicy(("api.example.test",), allowed_query_keys=query_keys)


class _Headers(dict[str, str]):
    def items(self):  # type: ignore[override]
        return super().items()


class _Response:
    def __init__(
        self,
        chunks: list[bytes],
        final_url: str,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.headers = _Headers(headers or {"Content-Type": "application/json"})
        self._chunks = iter(chunks)
        self._final_url = final_url
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _size: int) -> bytes:
        return next(self._chunks, b"")

    def geturl(self) -> str:
        return self._final_url

    def getcode(self) -> int:
        return self.status


class _OpenOnce:
    def __init__(self, *responses: _Response) -> None:
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, _timeout: int) -> _Response:
        self.requests.append(request)
        return self.responses.pop(0)


def test_acquire_preserves_response_metadata_and_redacts_secrets(monkeypatch) -> None:
    response = _Response(
        [b'{"ok":', b"true}"],
        "https://api.example.test/items?access_token=top-secret&signature=sign-me",
    )
    monkeypatch.setattr(common, "_open_once", _OpenOnce(response))

    result = common.acquire(
        "https://api.example.test/items?access_token=top-secret&signature=sign-me",
        policy=_policy(query_keys=("access_token", "signature")),
        resolver=lambda _host: ("8.8.8.8",),
    )

    assert result.status == 200
    assert result.body == b'{"ok":true}'
    assert result.final_url == "https://api.example.test/items?access_token=top-secret&signature=sign-me"
    assert result.headers == {"Content-Type": "application/json"}
    assert "top-secret" not in result.redacted_request_url
    assert "sign-me" not in result.redacted_request_url
    assert "access_token=%3Credacted%3E" in result.redacted_request_url
    assert "signature=%3Credacted%3E" in result.redacted_request_url


def test_acquire_rejects_disallowed_hosts_before_network(monkeypatch) -> None:
    opener = _OpenOnce()
    monkeypatch.setattr(common, "_open_once", opener)

    try:
        common.acquire(
            "https://other.example.test/",
            policy=_policy(),
            resolver=lambda _host: ("8.8.8.8",),
        )
    except PolicyError as error:
        assert "allowlisted" in str(error)
    else:
        raise AssertionError("expected allowlist rejection")
    assert not opener.requests


def test_acquire_rejects_oversized_response(monkeypatch) -> None:
    response = _Response([b"abc"], "https://api.example.test/items")
    monkeypatch.setattr(common, "_open_once", _OpenOnce(response))

    try:
        common.acquire(
            "https://api.example.test/items",
            max_bytes=2,
            policy=_policy(),
            resolver=lambda _host: ("8.8.8.8",),
        )
    except ValueError as error:
        assert "exceeds" in str(error)
    else:
        raise AssertionError("expected size rejection")


@pytest.mark.parametrize("status", (404, 500))
def test_acquire_rejects_non_successful_responses_before_reading_body(
    monkeypatch, status: int
) -> None:
    response = _Response([b'{"error":"not found"}'], "https://api.example.test/items", status=status)
    monkeypatch.setattr(common, "_open_once", _OpenOnce(response))

    with pytest.raises(ValueError, match="successful HTTP response"):
        common.get_json(
            "https://api.example.test/items",
            policy=_policy(),
            resolver=lambda _host: ("8.8.8.8",),
        )

    assert response.closed


def test_acquire_to_artifact_streams_multichunk_body_and_keeps_metadata(
    tmp_path, monkeypatch
) -> None:
    response = _Response(
        [b'{"ok":', b"true}"],
        "https://api.example.test/items?access_token=top-secret",
    )
    artifact_store = ArtifactStore(tmp_path)
    monkeypatch.setattr(common, "_open_once", _OpenOnce(response))
    monkeypatch.setattr(
        artifact_store,
        "put_bytes",
        lambda *_args, **_kwargs: pytest.fail("streamed acquisition must not buffer via put_bytes"),
    )

    result = common.acquire_to_artifact(
        "https://api.example.test/items?access_token=top-secret",
        artifact_store=artifact_store,
        policy=_policy(query_keys=("access_token",)),
        resolver=lambda _host: ("8.8.8.8",),
    )

    assert result.artifact.path.read_bytes() == b'{"ok":true}'
    assert artifact_store.verify(result.artifact)
    assert result.status == 200
    assert result.request_url == result.final_url
    assert "top-secret" not in result.redacted_request_url
    assert result.headers == {"Content-Type": "application/json"}


def test_acquire_to_artifact_retains_redacted_original_and_final_urls(
    tmp_path, monkeypatch
) -> None:
    opener = _OpenOnce(
        _Response(
            [],
            "https://api.example.test/items?access_token=first-secret",
            status=302,
            headers={"Location": "/final?signature=second-secret"},
        ),
        _Response(
            [b"ok"],
            "https://api.example.test/final?signature=second-secret",
        ),
    )
    monkeypatch.setattr(common, "_open_once", opener)

    result = common.acquire_to_artifact(
        "https://api.example.test/items?access_token=first-secret",
        artifact_store=ArtifactStore(tmp_path),
        policy=_policy(query_keys=("access_token", "signature")),
        resolver=lambda _host: ("8.8.8.8",),
    )

    assert result.request_url.endswith("access_token=first-secret")
    assert result.final_url.endswith("signature=second-secret")
    assert "first-secret" not in result.redacted_request_url
    assert "second-secret" not in result.redacted_final_url


def test_acquire_to_artifact_rejects_oversize_before_cas_publication(
    tmp_path, monkeypatch
) -> None:
    artifact_store = ArtifactStore(tmp_path)
    monkeypatch.setattr(
        common,
        "_open_once",
        _OpenOnce(_Response([b"abc", b"def"], "https://api.example.test/items")),
    )

    with pytest.raises(ArtifactTooLargeError):
        common.acquire_to_artifact(
            "https://api.example.test/items",
            artifact_store=artifact_store,
            max_bytes=4,
            policy=_policy(),
            resolver=lambda _host: ("8.8.8.8",),
        )

    assert artifact_store.published_digests() == ()
    assert list((tmp_path / "evidence" / ".tmp").iterdir()) == []


def test_acquire_to_artifact_rejects_unsafe_or_preflighted_content_before_publish(
    tmp_path, monkeypatch
) -> None:
    artifact_store = ArtifactStore(tmp_path)
    strict_policy = SourcePolicy(
        ("api.example.test",),
        artifact=ArtifactPolicy(allowed_media_types=("text/csv",)),
    )
    monkeypatch.setattr(
        common,
        "_open_once",
        _OpenOnce(
            _Response(
                [b"not a CSV"],
                "https://api.example.test/items",
                headers={"Content-Type": "application/pdf"},
            )
        ),
    )

    with pytest.raises(PolicyError, match="media type"):
        common.acquire_to_artifact(
            "https://api.example.test/items",
            artifact_store=artifact_store,
            policy=strict_policy,
            resolver=lambda _host: ("8.8.8.8",),
        )

    assert artifact_store.published_digests() == ()
    assert list((tmp_path / "evidence" / ".tmp").iterdir()) == []
    monkeypatch.setattr(
        common,
        "_open_once",
        _OpenOnce(_Response([b"ok"], "https://api.example.test/items")),
    )

    def reject_inactive_run(_metadata: common.AcquisitionMetadata) -> None:
        raise RuntimeError("inactive run")

    with pytest.raises(RuntimeError, match="inactive run"):
        common.acquire_to_artifact(
            "https://api.example.test/items",
            artifact_store=artifact_store,
            policy=_policy(),
            resolver=lambda _host: ("8.8.8.8",),
            before_publish=reject_inactive_run,
        )

    assert artifact_store.published_digests() == ()
    assert list((tmp_path / "evidence" / ".tmp").iterdir()) == []


def test_acquire_to_artifact_enforces_a_total_stream_deadline(
    tmp_path, monkeypatch
) -> None:
    artifact_store = ArtifactStore(tmp_path)
    monkeypatch.setattr(
        common,
        "_open_once",
        _OpenOnce(_Response([b"ok"], "https://api.example.test/items")),
    )
    moments = iter((0.0, 0.0, 2.0))

    with pytest.raises(TimeoutError, match="total deadline"):
        common.acquire_to_artifact(
            "https://api.example.test/items",
            artifact_store=artifact_store,
            policy=_policy(),
            resolver=lambda _host: ("8.8.8.8",),
            max_stream_seconds=1,
            monotonic_clock=lambda: next(moments),
        )

    assert artifact_store.published_digests() == ()


def test_acquire_validates_each_redirect_before_following_it(monkeypatch) -> None:
    opener = _OpenOnce(
        _Response(
            [],
            "https://api.example.test/items",
            status=302,
            headers={"Location": "https://127.0.0.1/private"},
        )
    )
    monkeypatch.setattr(common, "_open_once", opener)
    policy = SourcePolicy(("api.example.test",), max_redirects=1)

    with pytest.raises(PolicyError, match="private"):
        common.acquire(
            "https://api.example.test/items",
            policy=policy,
            resolver=lambda _host: ("8.8.8.8",),
        )

    assert len(opener.requests) == 1


def test_acquire_follows_only_policy_limited_validated_redirects(monkeypatch) -> None:
    opener = _OpenOnce(
        _Response(
            [],
            "https://api.example.test/items",
            status=302,
            headers={"Location": "/final"},
        ),
        _Response([b"ok"], "https://api.example.test/final"),
    )
    monkeypatch.setattr(common, "_open_once", opener)
    policy = SourcePolicy(("api.example.test",), max_redirects=1)

    result = common.acquire(
        "https://api.example.test/items",
        policy=policy,
        resolver=lambda _host: ("8.8.8.8",),
    )

    assert result.body == b"ok"
    assert [request.full_url for request in opener.requests] == [
        "https://api.example.test/items",
        "https://api.example.test/final",
    ]


def test_acquire_gates_each_redirect_and_defers_a_429(monkeypatch) -> None:
    events: list[tuple[object, ...]] = []
    responses = [
        _Response(
            [],
            "https://api.example.test/items",
            status=302,
            headers={"Location": "/final"},
        ),
        _Response(
            [],
            "https://api.example.test/final",
            status=429,
            headers={"Retry-After": "120"},
        ),
    ]

    def open_once(request, _timeout: int) -> _Response:
        events.append(("request", request.full_url))
        return responses.pop(0)

    class Gate:
        def permit(self, host: str, *, continuation: bool = False) -> None:
            events.append(("permit", host, continuation))

        def record_response(
            self, host: str, *, status: int, retry_after: str | None
        ) -> datetime | None:
            events.append(("response", host, status, retry_after))
            if status == 429:
                return datetime(2026, 8, 1, 12, tzinfo=UTC) + timedelta(seconds=120)
            return None

    monkeypatch.setattr(common, "_open_once", open_once)

    with pytest.raises(HostThrottleBlocked, match="api.example.test") as blocked:
        common.acquire(
            "https://api.example.test/items",
            policy=SourcePolicy(("api.example.test",), max_redirects=1),
            resolver=lambda _host: ("8.8.8.8",),
            host_gate=Gate(),
        )

    assert blocked.value.blocked_until == datetime(2026, 8, 1, 12, 2, tzinfo=UTC)
    assert events == [
        ("permit", "api.example.test", False),
        ("request", "https://api.example.test/items"),
        ("response", "api.example.test", 302, None),
        ("permit", "api.example.test", True),
        ("request", "https://api.example.test/final"),
        ("response", "api.example.test", 429, "120"),
    ]


@pytest.mark.parametrize(
    ("status", "headers"),
    [
        (204, {"Content-Type": "text/csv"}),
        (206, {"Content-Type": "text/csv", "Content-Range": "bytes 0-5/10"}),
        (200, {"Content-Type": "text/csv", "Content-Range": "bytes 0-5/10"}),
    ],
)
def test_acquire_to_artifact_can_require_a_complete_http_200_before_publish(
    tmp_path, monkeypatch, status: int, headers: dict[str, str]
) -> None:
    artifact_store = ArtifactStore(tmp_path)
    monkeypatch.setattr(
        common,
        "_open_once",
        _OpenOnce(_Response([b"DATE,IUDBEDR\n"], "https://api.example.test/items", status=status, headers=headers)),
    )

    with pytest.raises(ValueError, match="complete HTTP 200"):
        common.acquire_to_artifact(
            "https://api.example.test/items",
            artifact_store=artifact_store,
            policy=_policy(),
            resolver=lambda _host: ("8.8.8.8",),
            require_full_response=True,
        )

    assert artifact_store.published_digests() == ()
