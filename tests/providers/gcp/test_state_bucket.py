"""GCS state bucket helper tests (plan 4 B2 step 3), using a fake storage client so
nothing touches real GCP."""

from __future__ import annotations

from easymigrate.providers.gcp import state_bucket


class _FakeBucket:
    def __init__(self, name: str, exists: bool) -> None:
        self.name = name
        self._exists = exists
        self.versioning_enabled = False
        self.iam_configuration = type(
            "IamConfig", (), {"uniform_bucket_level_access_enabled": False}
        )()

    def exists(self) -> bool:
        return self._exists


class _FakeClient:
    def __init__(self, existing: set[str]) -> None:
        self._existing = existing
        self.created: list[tuple[str, str]] = []

    def bucket(self, name: str) -> _FakeBucket:
        return _FakeBucket(name, name in self._existing)

    def create_bucket(self, bucket: _FakeBucket, location: str) -> None:
        self.created.append((bucket.name, location))
        self._existing.add(bucket.name)


def test_bucket_name_format():
    assert state_bucket.bucket_name("my-project") == "my-project-easymigrate-tfstate"


def test_ensure_bucket_returns_true_when_already_exists_and_does_not_prompt():
    client = _FakeClient({"proj-easymigrate-tfstate"})
    prompted = []

    result = state_bucket.ensure_bucket(
        "proj", "proj-easymigrate-tfstate", "us-central1", prompted.append, client=client
    )

    assert result is True
    assert prompted == []
    assert client.created == []


def test_ensure_bucket_creates_with_versioning_and_uniform_access_when_confirmed():
    client = _FakeClient(set())

    result = state_bucket.ensure_bucket(
        "proj", "proj-easymigrate-tfstate", "us-central1", lambda name: True, client=client
    )

    assert result is True
    assert client.created == [("proj-easymigrate-tfstate", "us-central1")]


def test_ensure_bucket_returns_false_when_declined_and_does_not_create():
    client = _FakeClient(set())

    result = state_bucket.ensure_bucket(
        "proj", "proj-easymigrate-tfstate", "us-central1", lambda name: False, client=client
    )

    assert result is False
    assert client.created == []


class _Blob:
    def __init__(self, name: str, generation: int) -> None:
        self.name = name
        self.generation = generation


class _DeletableBucket:
    def __init__(self, client: _DeletingClient, name: str) -> None:
        self._client = client
        self.name = name

    def exists(self) -> bool:
        return self.name in self._client.existing

    def delete_blob(self, name: str, generation: int | None = None) -> None:
        self._client.deleted_blobs.append((name, generation))

    def delete(self) -> None:
        self._client.existing.discard(self.name)


class _DeletingClient:
    def __init__(self, existing: set[str], blobs: list[_Blob]) -> None:
        self.existing = existing
        self.blobs = blobs
        self.deleted_blobs: list[tuple[str, int | None]] = []

    def bucket(self, name: str) -> _DeletableBucket:
        return _DeletableBucket(self, name)

    def list_blobs(self, name: str, versions: bool = False):
        assert versions, "every noncurrent version must go too, or the bucket delete fails"
        return list(self.blobs)


def test_delete_bucket_removes_every_object_version_then_the_bucket():
    client = _DeletingClient(
        {"p-easymigrate-tfstate"},
        [_Blob("easymigrate/default.tfstate", 1), _Blob("easymigrate/default.tfstate", 2)],
    )

    assert state_bucket.delete_bucket("p", "p-easymigrate-tfstate", client=client) is True
    assert client.deleted_blobs == [
        ("easymigrate/default.tfstate", 1),
        ("easymigrate/default.tfstate", 2),
    ]
    assert client.existing == set()


def test_delete_bucket_that_does_not_exist_returns_false():
    client = _DeletingClient(set(), [])
    assert state_bucket.delete_bucket("p", "p-easymigrate-tfstate", client=client) is False
