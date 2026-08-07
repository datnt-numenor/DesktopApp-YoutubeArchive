from pathlib import Path

import boto3

import tasks
from storage import S3Storage


class FakeS3Client:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, str, str, dict[str, object]]] = []

    def upload_file(self, filename: str, bucket: str, key: str, **kwargs: object) -> None:
        self.uploads.append((filename, bucket, key, kwargs))

    def generate_presigned_url(self, ClientMethod: str, Params: dict[str, str], ExpiresIn: int) -> str:
        return f"https://cdn.example.test/{Params['Bucket']}/{Params['Key']}?method={ClientMethod}&ttl={ExpiresIn}"


def test_build_media_object_key_scopes_file_by_owner_and_playlist() -> None:
    assert (
        tasks.build_media_object_key("owner-1", 42, r"nested\video123.mp3")
        == "users/owner-1/playlists/42/video123.mp3"
    )


async def test_s3_storage_uploads_file_and_returns_object_key(tmp_path: Path) -> None:
    local_file = tmp_path / "song.mp3"
    local_file.write_bytes(b"mp3")
    fake_client = FakeS3Client()
    storage = S3Storage(client=fake_client, bucket_name="ytarchive-test")

    result = await storage.upload_file(local_file, "users/owner/playlists/1/song.mp3")

    assert result == "users/owner/playlists/1/song.mp3"
    assert fake_client.uploads == [
        (
            str(local_file),
            "ytarchive-test",
            "users/owner/playlists/1/song.mp3",
            {"ExtraArgs": {"ContentType": "audio/mpeg"}},
        )
    ]


async def test_s3_storage_generates_presigned_url(monkeypatch) -> None:
    fake_client = FakeS3Client()
    storage = S3Storage(client=fake_client, bucket_name="ytarchive-test")
    monkeypatch.setattr("storage.settings.s3_presigned_url_expiry", 123)

    url = await storage.get_file_url("users/owner/playlists/1/song.mp3")

    assert url == "https://cdn.example.test/ytarchive-test/users/owner/playlists/1/song.mp3?method=get_object&ttl=123"


def test_s3_storage_client_uses_r2_compatible_signature(monkeypatch) -> None:
    captured_kwargs = {}

    def fake_boto3_client(*_args, **kwargs):
        captured_kwargs.update(kwargs)
        return FakeS3Client()

    monkeypatch.setattr(boto3, "client", fake_boto3_client)
    monkeypatch.setattr("storage.settings.s3_endpoint_url", "https://example.r2.cloudflarestorage.com")
    monkeypatch.setattr("storage.settings.s3_access_key_id", "access-key")
    monkeypatch.setattr("storage.settings.s3_secret_access_key", "secret-key")

    storage = S3Storage(bucket_name="ytarchive-test")

    assert isinstance(storage.client, FakeS3Client)
    assert captured_kwargs["region_name"] == "auto"
    assert captured_kwargs["config"].signature_version == "s3v4"
