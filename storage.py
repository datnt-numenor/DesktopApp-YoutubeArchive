from pathlib import Path

from fastapi import HTTPException, status

from config import settings


class LocalStorage:
    """Store media under the data directory and reference it by a path relative to it."""

    async def upload_file(self, local_path: Path, object_key: str) -> str:
        return str(local_path.relative_to(settings.downloads_dir.parent))

    async def get_file_url(self, object_key: str) -> str:
        path = settings.downloads_dir.parent / object_key
        if not path.exists():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media file not found")
        return str(path)


storage = LocalStorage()
