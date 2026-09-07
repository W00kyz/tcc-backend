"""Push delivery seam (spec §2 Ruling 3). `notifications` is the canonical record; this seam
is only the best-effort fan-out on top of it. `LoggingPushSender` is the dev/CI implementation
— real FCM/APNs wiring is documented future work.

    push: PushSender = LoggingPushSender()
    await push.send(user_id=uid, title="Nova rota atribuída", body="...", data={})
"""

import logging
import uuid
from typing import Protocol

logger = logging.getLogger(__name__)


class PushSender(Protocol):
    async def send(
        self, *, user_id: uuid.UUID, title: str, body: str, data: dict[str, object]
    ) -> None: ...


class LoggingPushSender:
    """Dev/CI: logs the push, never raises, never hits the network. Real FCM/APNs delivery is
    documented future work (spec §2 Ruling 3)."""

    async def send(
        self, *, user_id: uuid.UUID, title: str, body: str, data: dict[str, object]
    ) -> None:
        logger.info(
            "push notification dispatched",
            extra={"event": "push_dispatched", "user_id": str(user_id), "title": title},
        )
