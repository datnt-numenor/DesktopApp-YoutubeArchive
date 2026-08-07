from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import crud  # noqa: E402
from database import AsyncSessionLocal, init_db  # noqa: E402
from downloader import extract_playlist_metadata  # noqa: E402
from models import Playlist, PlaylistVideo  # noqa: E402


async def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    await init_db()
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Playlist)
            .options(selectinload(Playlist.playlist_videos).selectinload(PlaylistVideo.video))
            .order_by(Playlist.id)
        )
        playlists = list(result.scalars().unique())
        for playlist in playlists:
            playlist_title = playlist.title
            try:
                metadata = await extract_playlist_metadata(playlist.url)
                await crud.sync_playlist_metadata(session, playlist, metadata)
                unavailable = sum(
                    1 for video in metadata["videos"] if not video.get("is_available_on_youtube", True)
                )
                print(f"Synced {playlist_title}: {len(metadata['videos'])} videos, {unavailable} unavailable")
            except Exception as exc:  # noqa: BLE001
                await session.rollback()
                print(f"Failed {playlist_title}: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
