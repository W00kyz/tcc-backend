import pytest
from app.core.object_store import MinioObjectStore, ObjectStoreError

from tests.support.object_store import FakeObjectStore


@pytest.mark.asyncio
async def test_fake_object_store_round_trips_bytes_and_content_type() -> None:
    store = FakeObjectStore()
    await store.put("evidence/a/b.jpg", b"\xff\xd8\xff", content_type="image/jpeg")

    data, content_type = await store.get("evidence/a/b.jpg")

    assert data == b"\xff\xd8\xff"
    assert content_type == "image/jpeg"


@pytest.mark.asyncio
async def test_fake_object_store_get_missing_raises() -> None:
    with pytest.raises(ObjectStoreError):
        await FakeObjectStore().get("nope")


@pytest.mark.asyncio
async def test_fake_object_store_delete_is_idempotent() -> None:
    store = FakeObjectStore()
    await store.delete("missing")  # no raise
    await store.put("k", b"x", content_type="text/plain")
    await store.delete("k")
    with pytest.raises(ObjectStoreError):
        await store.get("k")


def test_minio_object_store_constructs_without_connecting() -> None:
    store = MinioObjectStore(
        endpoint="minio:9000",
        access_key="k",
        secret_key="s",
        bucket="evidence",
        secure=False,
    )
    assert store.bucket == "evidence"
