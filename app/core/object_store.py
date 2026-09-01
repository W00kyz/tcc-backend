"""Object storage seam (spec §8). MinIO in prod, FakeObjectStore in tests — same thin
interface as the OSRM and QR crypto seams. The minio SDK is synchronous; put/get run it in
a thread so handlers stay async (asyncio.to_thread)."""

import asyncio
import io
from typing import Protocol

from minio import Minio
from minio.error import S3Error


class ObjectStoreError(Exception):
    pass


class ObjectStore(Protocol):
    async def put(self, key: str, data: bytes, *, content_type: str) -> None: ...
    async def get(self, key: str) -> tuple[bytes, str]: ...  # (data, content_type)
    async def delete(self, key: str) -> None: ...


class MinioObjectStore:
    def __init__(
        self,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        secure: bool,
    ) -> None:
        self.bucket = bucket
        self._client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)

    async def ensure_bucket(self) -> None:
        def _ensure() -> None:
            if not self._client.bucket_exists(self.bucket):
                self._client.make_bucket(self.bucket)

        await asyncio.to_thread(_ensure)

    async def put(self, key: str, data: bytes, *, content_type: str) -> None:
        def _put() -> None:
            self._client.put_object(
                self.bucket,
                key,
                io.BytesIO(data),
                length=len(data),
                content_type=content_type,
            )

        try:
            await asyncio.to_thread(_put)
        except S3Error as exc:  # pragma: no cover - network failure path
            raise ObjectStoreError(f"put {key!r} failed: {exc}") from exc

    async def get(self, key: str) -> tuple[bytes, str]:
        def _get() -> tuple[bytes, str]:
            response = self._client.get_object(self.bucket, key)
            try:
                return response.read(), response.headers.get(
                    "Content-Type", "application/octet-stream"
                )
            finally:
                response.close()
                response.release_conn()

        try:
            return await asyncio.to_thread(_get)
        except S3Error as exc:
            raise ObjectStoreError(f"get {key!r} failed: {exc}") from exc

    async def delete(self, key: str) -> None:
        def _delete() -> None:
            self._client.remove_object(self.bucket, key)

        try:
            await asyncio.to_thread(_delete)
        except S3Error as exc:  # pragma: no cover
            raise ObjectStoreError(f"delete {key!r} failed: {exc}") from exc
