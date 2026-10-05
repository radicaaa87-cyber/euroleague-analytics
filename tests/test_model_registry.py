from __future__ import annotations

import hashlib

import pytest

from euroleague.config import StorageSettings
from euroleague.model_registry import (
    ModelArtifactError,
    ModelArtifactStorage,
    artifact_path,
    validate_model_identity,
)


class _Response:
    def __init__(
        self,
        status_code: int,
        *,
        content: bytes = b"",
        payload: dict[str, object] | None = None,
    ) -> None:
        self.status_code = status_code
        self.content = content
        self._payload = payload or {}

    def json(self) -> dict[str, object]:
        return self._payload


class _Session:
    def __init__(self, *, get_responses: list[_Response], post_response: _Response) -> None:
        self.get_responses = list(get_responses)
        self.post_response = post_response
        self.posts: list[dict[str, object]] = []

    def get(self, _url: str, **_kwargs: object) -> _Response:
        return self.get_responses.pop(0)

    def post(self, url: str, **kwargs: object) -> _Response:
        self.posts.append({"url": url, **kwargs})
        return self.post_response


def _settings() -> StorageSettings:
    return StorageSettings(
        project_url="https://example.supabase.co",
        _service_key="secret",
        bucket="model-artifacts",
    )


def test_model_identity_rejects_path_escape() -> None:
    with pytest.raises(ValueError):
        validate_model_identity("player_points", "../v1", "a" * 40)


def test_artifact_path_is_version_scoped() -> None:
    assert artifact_path("player_points", "v1.2.3") == "player_points/v1.2.3/model.pkl"


def test_storage_refuses_public_bucket() -> None:
    session = _Session(
        get_responses=[_Response(200, payload={"public": True})],
        post_response=_Response(500),
    )
    storage = ModelArtifactStorage(_settings(), session=session)

    with pytest.raises(ModelArtifactError, match="public"):
        storage.assert_private_bucket()


def test_duplicate_upload_is_accepted_only_when_bytes_match() -> None:
    body = b"immutable-model"
    checksum = hashlib.sha256(body).hexdigest()
    session = _Session(
        get_responses=[_Response(200, content=body)],
        post_response=_Response(409),
    )
    storage = ModelArtifactStorage(_settings(), session=session)

    storage.upload_immutable("player_points/v1/model.pkl", body, checksum)

    assert session.posts[0]["headers"]["x-upsert"] == "false"
