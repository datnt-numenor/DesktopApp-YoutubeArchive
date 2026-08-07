import asyncio
import mimetypes
from pathlib import Path
from typing import Any

from fastapi import HTTPException, status

from config import settings


class StorageBackend:
    async def upload_file(self, local_path: Path, object_key: str) -> str:
        raise NotImplementedError

    async def get_file_url(self, object_key: str) -> str:
        raise NotImplementedError


class LocalStorage(StorageBackend):
    async def upload_file(self, local_path: Path, object_key: str) -> str:
        return str(local_path.relative_to(settings.downloads_dir.parent))

    async def get_file_url(self, object_key: str) -> str:
        path = settings.downloads_dir.parent / object_key
        if not path.exists():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media file not found")
        return str(path)


class S3Storage(StorageBackend):
    def __init__(self, client: Any | None = None, bucket_name: str | None = None) -> None:
        self._client = client
        self.bucket_name = bucket_name or settings.s3_bucket_name
        if not self.bucket_name:
            raise RuntimeError("S3_BUCKET_NAME is required when using S3/R2 storage")

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3
            from botocore.config import Config

            self._client = boto3.client(
                "s3",
                endpoint_url=settings.s3_endpoint_url,
                aws_access_key_id=settings.s3_access_key_id,
                aws_secret_access_key=settings.s3_secret_access_key,
                region_name="auto",
                config=Config(signature_version="s3v4"),
            )
        return self._client

    async def upload_file(self, local_path: Path, object_key: str) -> str:
        content_type, _ = mimetypes.guess_type(local_path.name)
        extra_args = {"ContentType": content_type} if content_type else None

        def upload() -> None:
            kwargs: dict[str, Any] = {}
            if extra_args:
                kwargs["ExtraArgs"] = extra_args
            self.client.upload_file(str(local_path), self.bucket_name, object_key, **kwargs)

        await asyncio.to_thread(upload)
        return object_key

    async def get_file_url(self, object_key: str) -> str:
        def create_url() -> str:
            return self.client.generate_presigned_url(
                ClientMethod="get_object",
                Params={"Bucket": self.bucket_name, "Key": object_key},
                ExpiresIn=settings.s3_presigned_url_expiry,
            )

        return await asyncio.to_thread(create_url)


def get_storage_backend() -> StorageBackend:
    if settings.resolved_storage_backend == "s3":
        return S3Storage()
    return LocalStorage()


storage = get_storage_backend()
