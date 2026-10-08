"""Text in, bot speech text out, with durable sessions and the shared core."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import replace

from app.api_store import ApiStore
from app.config import Settings
from app.contracts.booking import normalize_phone
from app.contracts.chat import MessageInput
from app.runtime import conversation_runtime
from app.workers.coordinator import Coordinator


class TextBot:
    """Use as an async context manager for a long-lived speech/text integration.

    Returning text is delivery to this adapter's caller; the next input acknowledges
    that output. A TTS integration must pass acknowledge_previous=False until the
    utterance has actually been played. One runtime/process per database pair.
    """

    def __init__(self, settings: Settings | None = None):
        if settings is None:
            loaded = Settings.load()
            # Text callers do not compete with a running HTTP worker for sessions.
            settings = replace(
                loaded,
                database_path=loaded.database_path.parent / "text.sqlite",
                checkpoint_path=loaded.checkpoint_path.parent / "text_checkpoints.sqlite",
            )
        self.settings = settings
        self._context = None
        self._locks = {}

    async def __aenter__(self):
        self._context = conversation_runtime(self.settings)
        self.engine, self.graph = await self._context.__aenter__()
        self.store = ApiStore(self.settings.database_path)
        self.coordinator = Coordinator(self.store, self.graph)
        self._runner = asyncio.create_task(self.coordinator.run())
        return self

    async def __aexit__(self, *args):
        await self.coordinator.close()
        self._runner.cancel()
        await asyncio.gather(self._runner, return_exceptions=True)
        await self._context.__aexit__(*args)

    async def ask(
        self,
        customer_text: str,
        *,
        session_id: str = "default",
        message_id: str | None = None,
        acknowledge_previous: bool = True,
        timeout_seconds: float = 120,
        customer_phone: str | None = None,
        customer_name: str | None = None,
    ) -> str:
        if not self._context:
            raise RuntimeError("Use 'async with TextBot(...) as bot'")
        if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 128:
            raise ValueError("session_id must be a nonempty string of at most 128 characters")
        message = MessageInput(client_message_id=message_id or uuid.uuid4().hex, text=customer_text)
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            sid = self.store.find_session("local-text", session_id)
            if sid is None:
                if customer_phone is None or customer_name is None:
                    raise ValueError("Nhập customer_phone và customer_name để bắt đầu phiên")
                sid = str(uuid.uuid4())
                state = self.engine.new_state(sid, customer_phone=customer_phone,
                                              customer_name=customer_name)
                await self.graph.initialize(sid, state)
                self.store.create_session(sid, "local-text", session_id, state)
            snapshot = self.store.snapshot(sid)
            profile = snapshot["state"]
            if ((customer_phone is not None and normalize_phone(customer_phone) != profile.get("customer_phone"))
                    or (customer_name is not None and customer_name.strip() != profile.get("customer_name"))):
                raise ValueError("Phiên đã thuộc khách hàng khác. Dùng khóa phiên mới cho khách hàng này.")
            response = snapshot["state"].get("last_response")
            if response and acknowledge_previous:
                message.reply_to_response_id = response["response_id"]
                message.rendered_response_ids = [response["response_id"]]
            with self.store.connection() as db:
                old = db.execute(
                    "SELECT payload FROM api_inbox WHERE session_id=? AND kind='message' AND client_key=?",
                    (sid, message.client_message_id),
                ).fetchone()
            if old:
                original = json.loads(old["payload"])
                message.reply_to_response_id = original.get("reply_to_response_id")
            receipt = self.store.enqueue(
                sid, "message", message.client_message_id, message.model_dump()
            )
            self.coordinator.wakeup.set()
            response_event_id = receipt["event_id"] + ":response"
            deadline = asyncio.get_running_loop().time() + timeout_seconds
            while asyncio.get_running_loop().time() < deadline:
                with self.store.connection() as db:
                    row = db.execute(
                        "SELECT payload FROM api_events WHERE session_id=? AND id=?",
                        (sid, response_event_id),
                    ).fetchone()
                if row:
                    return json.loads(row["payload"])["text"]
                snapshot = self.store.snapshot(sid)
                if snapshot["needs_support"]:
                    raise RuntimeError(
                        "The session needs support; its pending transaction has been preserved"
                    )
                await asyncio.sleep(0.05)
            raise TimeoutError(
                "The input remains saved; retry with the same message_id to read its result"
            )


async def main_async(
    customer_text: str,
    *,
    session_id: str = "default",
    settings: Settings | None = None,
    message_id: str | None = None,
    customer_phone: str | None = None,
    customer_name: str | None = None,
) -> str:
    async with TextBot(settings) as bot:
        return await bot.ask(customer_text, session_id=session_id, message_id=message_id,
                             customer_phone=customer_phone, customer_name=customer_name)


def main(
    customer_text: str,
    *,
    session_id: str = "default",
    settings: Settings | None = None,
    message_id: str | None = None,
    customer_phone: str | None = None,
    customer_name: str | None = None,
) -> str:
    """Return the bot's spoken text. Reuse session_id to retain conversation state."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            main_async(
                customer_text, session_id=session_id, settings=settings, message_id=message_id,
                customer_phone=customer_phone, customer_name=customer_name
            )
        )
    raise RuntimeError(
        "Inside an async application use 'await main_async(...)' or TextBot.ask(...)"
    )
