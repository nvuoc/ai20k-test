"""HTTP client used by voice workers; the API remains the sole booking owner."""
import asyncio
import uuid

import httpx


class BookingBridge:
    def __init__(self, base_url: str, secret: str, session_id: str, room: str):
        self.path = f"/api/voice-agent/sessions/{session_id}"
        self.client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=15,
            headers={"Authorization": "Bearer " + secret, "X-Voice-Room": room})
        self.previous = None

    async def close(self):
        await self.client.aclose()

    async def snapshot(self):
        response = await self.client.get(self.path)
        response.raise_for_status()
        return response.json()

    async def resume(self, *, timeout: float = 120):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            view = await self.snapshot()
            if view.get("needs_support"):
                raise RuntimeError("Phiên cần nhân viên hỗ trợ.")
            if not view["pending_count"]:
                return view
            await asyncio.sleep(0.25)
        raise TimeoutError("Phiên vẫn đang xử lý yêu cầu trước.")

    async def ask(self, text: str, *, timeout: float = 120):
        body = {"client_message_id": uuid.uuid4().hex, "text": text,
                "reply_to_response_id": self.previous,
                "rendered_response_ids": []}
        deadline = asyncio.get_running_loop().time() + timeout
        # Retry admission with the SAME id; ambiguous network failures cannot duplicate a turn.
        for attempt in range(3):
            try:
                result = await self.client.post(self.path + "/messages", json=body)
                result.raise_for_status()
                break
            except (httpx.TransportError, httpx.HTTPStatusError) as error:
                if isinstance(error, httpx.HTTPStatusError) and error.response.status_code < 500:
                    raise
                if attempt == 2:
                    raise
                await asyncio.sleep(0.3 * (attempt + 1))
        event_id = result.json()["event_id"]
        cursor = 0
        while asyncio.get_running_loop().time() < deadline:
            result = await self.client.get(self.path, params={"after_cursor": cursor})
            result.raise_for_status()
            view = result.json()
            for event in view["events"]:
                if event["event_id"] == event_id + ":response":
                    return event["payload"]
                if event["event_id"] == event_id + ":failed":
                    raise RuntimeError("Không xử lý được lượt nói; thông tin đã được lưu.")
            if view.get("needs_support"):
                raise RuntimeError("Phiên cần nhân viên hỗ trợ.")
            cursor = view["next_cursor"]
            if not view["has_more"]:
                await asyncio.sleep(0.25)
        raise TimeoutError("Lượt nói đang được xử lý; vui lòng kết nối lại để nghe kết quả.")

    async def played(self, response: dict):
        result = await self.client.post(self.path + "/delivery-acks", json={
            "ack_id": "speech-" + response["response_id"],
            "response_id": response["response_id"], "generation": response["generation"],
            "delivery_type": "rendered",
        })
        result.raise_for_status()
        self.previous = response["response_id"]
