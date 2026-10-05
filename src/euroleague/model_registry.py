"""Persistent model artifacts and registry writes for trained candidates."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from euroleague.config import StorageSettings

MODEL_ARTIFACT_BUCKET = "model-artifacts"
_FAMILY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class ModelArtifactError(RuntimeError):
    """Raised when a trained model cannot be stored or verified safely."""


class ModelRegistryConflictError(RuntimeError):
    """Raised when one immutable model version is reused with different contents."""


def validate_model_identity(model_family: str, version: str, git_commit: str) -> None:
    """Reject identifiers that could escape an artifact path or weaken lineage."""
    if not _FAMILY_RE.fullmatch(model_family):
        raise ValueError("model_family must use lowercase letters, digits, underscores or hyphens.")
    if not _VERSION_RE.fullmatch(version):
        raise ValueError("version must use letters, digits, dots, underscores or hyphens.")
    if not _GIT_SHA_RE.fullmatch(git_commit):
        raise ValueError("git_commit must be a full 40-character lowercase Git SHA.")


def file_sha256(path: Path) -> str:
    """Return the SHA-256 of one artifact without loading it all into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_path(model_family: str, version: str) -> str:
    """Return the immutable object path for one model version."""
    return f"{model_family}/{version}/model.pkl"


class ModelArtifactStorage:
    """Minimal private Supabase Storage client for immutable model artifacts."""

    def __init__(
        self,
        settings: StorageSettings,
        *,
        session: requests.Session | Any | None = None,
        timeout_seconds: int = 120,
    ) -> None:
        self.settings = settings
        self.session = session or requests.Session()
        self.timeout_seconds = timeout_seconds

    def _headers(self) -> dict[str, str]:
        key = self.settings.service_key()
        return {"Authorization": f"Bearer {key}", "apikey": key}

    def _bucket_url(self) -> str:
        bucket = quote(self.settings.bucket, safe="")
        return f"{self.settings.project_url}/storage/v1/bucket/{bucket}"

    def _object_url(self, path: str) -> str:
        bucket = quote(self.settings.bucket, safe="")
        encoded = "/".join(quote(part, safe="") for part in path.split("/"))
        return f"{self.settings.project_url}/storage/v1/object/{bucket}/{encoded}"

    def assert_private_bucket(self) -> None:
        """Require the pre-created artifact bucket and refuse a public bucket."""
        response = self.session.get(
            self._bucket_url(),
            headers=self._headers(),
            timeout=self.timeout_seconds,
        )
        if not 200 <= response.status_code < 300:
            raise ModelArtifactError(
                f"Could not inspect model artifact bucket: HTTP {response.status_code}."
            )
        if bool(response.json().get("public")):
            raise ModelArtifactError(
                f"Storage bucket {self.settings.bucket!r} is public; refusing model upload."
            )

    def download_verified(self, path: str, expected_sha256: str) -> bytes:
        """Download one model and prove its bytes match the registry checksum."""
        response = self.session.get(
            self._object_url(path),
            headers=self._headers(),
            timeout=self.timeout_seconds,
        )
        if not 200 <= response.status_code < 300:
            raise ModelArtifactError(
                f"Could not download model artifact {path!r}: HTTP {response.status_code}."
            )
        actual = hashlib.sha256(response.content).hexdigest()
        if actual != expected_sha256:
            raise ModelArtifactError(
                f"Stored model artifact {path!r} has checksum {actual}, "
                f"expected {expected_sha256}."
            )
        return response.content

    def upload_immutable(self, path: str, body: bytes, expected_sha256: str) -> None:
        """Upload once; if the path exists, require byte-for-byte identity."""
        actual = hashlib.sha256(body).hexdigest()
        if actual != expected_sha256:
            raise ModelArtifactError(
                f"Local model checksum {actual} does not match expected {expected_sha256}."
            )

        response = self.session.post(
            self._object_url(path),
            headers={
                **self._headers(),
                "Content-Type": "application/octet-stream",
                "x-upsert": "false",
            },
            data=body,
            timeout=self.timeout_seconds,
        )
        if 200 <= response.status_code < 300:
            return

        try:
            self.download_verified(path, expected_sha256)
        except ModelArtifactError:
            raise ModelArtifactError(
                f"Could not upload immutable model artifact {path!r}: "
                f"HTTP {response.status_code}."
            ) from None


def register_candidate(
    connection: Any,
    *,
    model_family: str,
    version: str,
    artifact_bucket: str,
    artifact_path_value: str,
    artifact_sha256: str,
    artifact_size_bytes: int,
    git_commit: str,
    framework: str,
    framework_version: str,
    model_class: str,
    trained_at: str,
    train_seasons: list[str],
    validation_seasons: list[str],
    blind_test_seasons: list[str],
    feature_names: list[str],
    selected_params: dict[str, Any],
    metrics: dict[str, Any],
    training_metadata: dict[str, Any],
) -> str:
    """Insert one immutable candidate, or accept an exact idempotent replay."""
    validate_model_identity(model_family, version, git_commit)
    if artifact_sha256 != artifact_sha256.lower() or not re.fullmatch(
        r"[0-9a-f]{64}", artifact_sha256
    ):
        raise ValueError("artifact_sha256 must be a lowercase SHA-256 hex digest.")
    if artifact_size_bytes <= 0:
        raise ValueError("artifact_size_bytes must be positive.")
    if not train_seasons:
        raise ValueError("train_seasons may not be empty.")
    if not feature_names:
        raise ValueError("feature_names may not be empty.")

    query = """
        insert into public.model_registry (
            model_family,
            version,
            status,
            artifact_bucket,
            artifact_path,
            artifact_sha256,
            artifact_size_bytes,
            git_commit,
            framework,
            framework_version,
            model_class,
            trained_at,
            train_seasons,
            validation_seasons,
            blind_test_seasons,
            feature_names,
            selected_params,
            metrics,
            training_metadata
        )
        values (
            %s, %s, 'candidate', %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb
        )
        on conflict (model_family, version) do update
        set version = excluded.version
        where model_registry.artifact_bucket = excluded.artifact_bucket
          and model_registry.artifact_path = excluded.artifact_path
          and model_registry.artifact_sha256 = excluded.artifact_sha256
          and model_registry.artifact_size_bytes = excluded.artifact_size_bytes
          and model_registry.git_commit = excluded.git_commit
          and model_registry.framework = excluded.framework
          and model_registry.framework_version is not distinct from excluded.framework_version
          and model_registry.model_class = excluded.model_class
          and model_registry.trained_at = excluded.trained_at
          and model_registry.train_seasons = excluded.train_seasons
          and model_registry.validation_seasons = excluded.validation_seasons
          and model_registry.blind_test_seasons = excluded.blind_test_seasons
          and model_registry.feature_names = excluded.feature_names
          and model_registry.selected_params = excluded.selected_params
          and model_registry.metrics = excluded.metrics
          and model_registry.training_metadata = excluded.training_metadata
        returning status
    """
    values = (
        model_family,
        version,
        artifact_bucket,
        artifact_path_value,
        artifact_sha256,
        artifact_size_bytes,
        git_commit,
        framework,
        framework_version,
        model_class,
        trained_at,
        train_seasons,
        validation_seasons,
        blind_test_seasons,
        feature_names,
        json.dumps(selected_params, sort_keys=True),
        json.dumps(metrics, sort_keys=True),
        json.dumps(training_metadata, sort_keys=True),
    )
    with connection.cursor() as cursor:
        cursor.execute(query, values)
        row = cursor.fetchone()
    if row is None:
        raise ModelRegistryConflictError(
            f"Model {model_family}/{version} already exists with different immutable metadata."
        )
    return str(row[0])
