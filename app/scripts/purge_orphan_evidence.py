"""Deletes evidence photos that no `evidence_items` row points at any more.

Usage: uv run python -m app.scripts.purge_orphan_evidence

Run it once after the automatic-allocation reset migration (f3b9d1a7c2e5): that migration wipes
every evidence row, but Alembic cannot reach MinIO, so the photo objects stay behind. With the
table empty every object in the bucket is an orphan. Keys still referenced by a row are never
touched, so running it again later is safe.
"""

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.object_store import ListableObjectStore, MinioObjectStore
from app.db.session import build_engine, build_session_factory
from app.domain.execution.models import EvidenceItem

logger = logging.getLogger(__name__)


async def purge_orphan_evidence(db: AsyncSession, store: ListableObjectStore) -> list[str]:
    """Delete every object whose key no evidence row references; returns the deleted keys.

    Example: ``deleted = await purge_orphan_evidence(session, minio_store)``
    """
    referenced = set(
        (
            await db.scalars(
                select(EvidenceItem.object_key).where(EvidenceItem.object_key.is_not(None))
            )
        ).all()
    )
    orphans = [key for key in await store.list_keys() if key not in referenced]
    for key in orphans:
        await store.delete(key)
        logger.info("orphan evidence object deleted", extra={"event": "evidence_orphan_purged"})
    return orphans


async def _main() -> None:
    settings = get_settings()
    store = MinioObjectStore(
        endpoint=settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        bucket=settings.minio_bucket_evidence,
        secure=settings.minio_secure,
    )
    engine = build_engine(settings.database_url)
    try:
        async with build_session_factory(engine)() as db:
            deleted = await purge_orphan_evidence(db, store)
    finally:
        await engine.dispose()
    print(f"Deleted {len(deleted)} orphan evidence object(s) from bucket {store.bucket!r}.")


if __name__ == "__main__":
    asyncio.run(_main())
