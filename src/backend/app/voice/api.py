"""Browser room credentials and authenticated worker bridge (no second DB worker)."""
from __future__ import annotations

import hmac
import json
import uuid
from datetime import timedelta

from fastapi import HTTPException, Request

from app.contracts.chat import AckInput, ChatSnapshot, DeliveryAck, MessageInput, Receipt


def install_voice_routes(app, *, owned, project):
    # A reconnect supersedes the old worker. One API process owns SQLite already.
    rooms: dict[str, str] = {}

    def worker(request, session_id):
        config = app.state.config
        supplied = request.headers.get("authorization", "")
        if (not config.voice_enabled or not config.voice_agent_secret
                or not hmac.compare_digest(supplied, "Bearer " + config.voice_agent_secret)):
            raise HTTPException(401, "Agent không được phép truy cập.")
        if rooms.get(session_id) != request.headers.get("x-voice-room"):
            raise HTTPException(409, "Kết nối giọng nói đã hết hiệu lực; hãy kết nối lại.")

    @app.post("/api/sessions/{session_id}/voice")
    async def connect_voice(request: Request, session_id: str):
        owned(request, session_id)
        config = app.state.config
        if not config.voice_enabled or not all((config.livekit_url, config.livekit_api_key,
                                                config.livekit_api_secret, config.voice_agent_secret)):
            raise HTTPException(503, "Chưa cấu hình LiveKit và VOICE_AGENT_SECRET trên máy chủ.")
        if len(config.voice_agent_secret) < 32:
            raise HTTPException(503, "VOICE_AGENT_SECRET cần ít nhất 32 ký tự.")
        from livekit import api
        room = "parrotgo-" + uuid.uuid4().hex
        identity = "customer-" + session_id
        metadata = json.dumps({"session_id": session_id, "room": room, "identity": identity})
        token = (api.AccessToken(config.livekit_api_key, config.livekit_api_secret)
                 .with_identity(identity).with_ttl(timedelta(minutes=10))
                 .with_grants(api.VideoGrants(room_join=True, room=room, can_subscribe=True,
                                             can_publish=True, can_publish_data=False,
                                             can_publish_sources=["microphone"]))
                 .with_room_config(api.RoomConfiguration(agents=[api.RoomAgentDispatch(
                     agent_name=config.livekit_agent_name, metadata=metadata)]))
                 .to_jwt())
        rooms[session_id] = room
        return {"server_url": config.livekit_url, "participant_token": token, "room": room}

    @app.get("/api/voice-agent/sessions/{session_id}", response_model=ChatSnapshot)
    async def snapshot(request: Request, session_id: str, after_cursor: int = 0):
        worker(request, session_id)
        return project(app.state.store.snapshot(session_id, after=after_cursor))

    @app.post("/api/voice-agent/sessions/{session_id}/messages", status_code=202,
              response_model=Receipt)
    async def message(request: Request, session_id: str, body: MessageInput):
        worker(request, session_id)
        # Workers cannot declare delivery through STT input; only playout ACKs do.
        if body.rendered_response_ids:
            raise HTTPException(422, "Agent phải xác nhận phát âm thanh bằng delivery-acks.")
        receipt = app.state.store.enqueue(session_id, "message", body.client_message_id,
                                          body.model_dump())
        state = app.state.store.snapshot(session_id, limit=1)["state"]
        receipt["api_version"] = "chat-api-3" if state["control"].get("schema_version", 4) >= 6 else "chat-api-2"
        app.state.coordinator.wakeup.set()
        return receipt

    @app.post("/api/voice-agent/sessions/{session_id}/delivery-acks", response_model=DeliveryAck)
    async def acknowledge(request: Request, session_id: str, body: AckInput):
        worker(request, session_id)
        app.state.store.ack(session_id, body.model_dump())
        return {"status": "acknowledged"}
