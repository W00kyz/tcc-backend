from app.core.object_store import ObjectStoreError


class FakeObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    async def put(self, key: str, data: bytes, *, content_type: str) -> None:
        self.objects[key] = (data, content_type)

    async def get(self, key: str) -> tuple[bytes, str]:
        try:
            return self.objects[key]
        except KeyError as exc:
            raise ObjectStoreError(f"no object at {key!r}") from exc

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)
