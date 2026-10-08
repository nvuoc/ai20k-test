"""Voice transport boundaries, spoken choices and provider ordering, offline."""
import asyncio
import json
from dataclasses import replace

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from app.adapters.map_vietmap import VietMapAdapter
from app.config import Settings
from app.main import create_app
from app.voice.bridge import BookingBridge
from app.voice.selection import match_candidate


@pytest.fixture
def voice_client(tmp_path):
    settings = Settings(profile="test", secret="test-only-app-secret",
                        database_path=tmp_path / "app.sqlite",
                        checkpoint_path=tmp_path / "checkpoints.sqlite")
    settings = replace(settings, voice_enabled=True, livekit_url="wss://voice.example.test",
                       livekit_api_key="test-key", livekit_api_secret="test-livekit-secret-32-characters",
                       voice_agent_secret="test-worker-secret-32-characters-long")
    with TestClient(create_app(settings)) as client:
        client.get("/api/bootstrap")
        view = client.post("/api/sessions", json={"client_session_key": "voice-test",
            "customer_phone": "0901234567", "customer_name": "An"}).json()
        yield client, view["session_id"], settings


def test_browser_token_is_room_scoped_and_secrets_stay_server_side(voice_client):
    client, sid, settings = voice_client
    public = client.get("/api/bootstrap").json()
    assert public["capabilities"]["voice_booking"] is True
    assert public["capabilities"]["text_only_chat"] is False
    credentials = client.post(f"/api/sessions/{sid}/voice").json()
    token = jwt.decode(credentials["participant_token"], settings.livekit_api_secret,
                       algorithms=["HS256"], issuer=settings.livekit_api_key)
    assert token["video"]["room"] == credentials["room"]
    assert token["video"]["canPublishSources"] == ["microphone"]
    assert token["video"]["canPublishData"] is False
    dispatch = token["roomConfig"]["agents"][0]
    assert dispatch["agentName"] == settings.livekit_agent_name
    assert json.loads(dispatch["metadata"])["session_id"] == sid
    assert settings.voice_agent_secret not in json.dumps(token)
    assert settings.livekit_api_secret not in json.dumps(public)
    client.cookies.clear()
    assert client.post(f"/api/sessions/{sid}/voice").status_code == 401


def test_bridge_requires_service_auth_and_reconnect_revokes_old_room(voice_client):
    client, sid, settings = voice_client
    url = f"/api/voice-agent/sessions/{sid}"
    room = client.post(f"/api/sessions/{sid}/voice").json()["room"]
    headers = {"Authorization": "Bearer " + settings.voice_agent_secret, "X-Voice-Room": room}
    assert client.get(url).status_code == 401
    assert client.get(url, headers=headers).status_code == 200
    assert client.post(url + "/messages", headers=headers, json={
        "client_message_id": "invalid-delivery", "text": "đúng",
        "rendered_response_ids": ["not-spoken"]}).status_code == 422
    newer = client.post(f"/api/sessions/{sid}/voice").json()["room"]
    assert newer != room
    assert client.get(url, headers=headers).status_code == 409
    headers["X-Voice-Room"] = newer
    assert client.get(url, headers=headers).status_code == 200


def test_disabled_voice_returns_clear_configuration_error(tmp_path):
    settings = Settings(profile="test", secret="test-only",
        database_path=tmp_path / "app.sqlite", checkpoint_path=tmp_path / "checkpoints.sqlite")
    with TestClient(create_app(settings)) as client:
        client.get("/api/bootstrap")
        sid = client.post("/api/sessions", json={"client_session_key": "disabled",
            "customer_phone": "0901234567", "customer_name": "An"}).json()["session_id"]
        assert client.post(f"/api/sessions/{sid}/voice").status_code == 503


@pytest.mark.parametrize("text,index", [("thứ nhất", 0), ("cái thứ hai", 1),
    ("chọn số 2", 1), ("đầu tiên", 0), ("Trường Sao Mai cơ sở B", 1),
    ("Truong Sao Mai co so A", 0), ("Trường Sao Mai", None),
    ("không chọn thứ hai", None), ("thứ ba", None),
    ("chọn thứ hai rồi đổi điểm đón", None)])
def test_spoken_choices_do_not_guess_shared_names(text, index):
    candidates = [{"label": "Trường Sao Mai cơ sở A", "candidate_id": "a"},
                  {"label": "Trường Sao Mai cơ sở B", "candidate_id": "b"}]
    matched = match_candidate(text, candidates)
    assert matched == (candidates[index] if index is not None else None)


def test_voice_search_never_fetches_third_result_or_expands_entrances():
    calls = []

    def handler(request):
        if "search" in request.url.path:
            return httpx.Response(200, json=[
                {"ref_id": "first", "display": "Trường Sao Mai A", "name": "Trường Sao Mai A",
                 "entry_points": [{"ref_id": "gate", "name": "Cổng 1"}]},
                {"ref_id": "second", "display": "Trường Sao Mai B", "name": "Trường Sao Mai B"},
                {"ref_id": "third", "display": "Trường Sao Mai C", "name": "Trường Sao Mai C"},
            ])
        calls.append(request.url.params["refid"])
        return httpx.Response(200, json={"lat": 21.02, "lng": 105.85, "city": "Hà Nội"})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            maps = VietMapAdapter("offline-test-key", client=client, voice_top_two=True)
            result = await maps.resolve("Trường Sao Mai")
            assert result["status"] == "ambiguous"
            assert [row["id"] for row in result["candidates"]] == ["first", "second"]
    asyncio.run(scenario())
    assert calls == ["first", "second"]


def test_http_bridge_retries_same_admission_id_and_does_not_claim_delivery():
    admitted = []
    acknowledgements = []
    reply = {"response_id": "reply-one", "generation": 1, "text": "Bạn chọn địa điểm nào?"}

    def handler(request):
        if request.url.path.endswith("/messages"):
            body = json.loads(request.content)
            admitted.append(body)
            if len(admitted) == 1:
                return httpx.Response(503)
            return httpx.Response(202, json={"event_id": "event-one"})
        if request.url.path.endswith("/delivery-acks"):
            acknowledgements.append(json.loads(request.content))
            return httpx.Response(200, json={"status": "acknowledged"})
        return httpx.Response(200, json={"events": [{"event_id": "event-one:response", "payload": reply}],
                                        "next_cursor": 2, "has_more": False, "needs_support": False})

    async def scenario():
        bridge = BookingBridge("https://api.example.test", "secret", "sid", "room")
        await bridge.client.aclose()
        bridge.client = httpx.AsyncClient(base_url="https://api.example.test",
                                         transport=httpx.MockTransport(handler))
        assert await bridge.ask("thứ hai") == reply
        assert not acknowledgements and bridge.previous is None
        await bridge.played(reply)
        assert bridge.previous == "reply-one"
        await bridge.close()
    asyncio.run(scenario())
    assert admitted[0] == admitted[1]
    assert admitted[0]["rendered_response_ids"] == []
    assert acknowledgements[0]["response_id"] == "reply-one"


def test_azure_plugin_parameters_and_synthesis_endpoint(monkeypatch):
    pytest.importorskip("livekit.plugins.azure")
    from app.voice.agent import speech_plugins

    monkeypatch.setenv("AZURE_SPEECH_KEY", "offline-test-key")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "southeastasia")
    monkeypatch.setenv("AZURE_SPEECH_ENDPOINT", "https://southeastasia.api.cognitive.microsoft.com/")
    monkeypatch.delenv("AZURE_TTS_ENDPOINT", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_HOST", raising=False)
    stt, tts = speech_plugins()
    assert stt is not None
    assert tts._opts.get_endpoint_url() == "https://southeastasia.tts.speech.microsoft.com/cognitiveservices/v1"
    assert tts._opts.voice == "vi-VN-HoaiMyNeural"


@pytest.mark.parametrize("interrupted,audio_error,expected", [(False, False, 1),
    (True, False, 0), (False, True, 0)])
def test_ack_requires_completed_uninterrupted_audio(interrupted, audio_error, expected):
    pytest.importorskip("livekit.plugins.azure")
    from app.voice.agent import BookingAgent

    played = []

    class Bridge:
        async def played(self, response):
            played.append(response)

    class Speech:
        async def wait_for_playout(self):
            self.completed = True

    speech = Speech()
    speech.interrupted = interrupted

    class Session:
        def say(self, text, **kwargs):
            assert kwargs["allow_interruptions"] is False
            agent.audio_failed = audio_error
            return speech

    class TestAgent(BookingAgent):
        @property
        def session(self):
            return Session()

    async def scenario():
        nonlocal agent
        agent = TestAgent(Bridge())
        await agent.speak({"text": "Xác nhận chuyến đi"})

    agent = None
    asyncio.run(scenario())
    assert speech.completed
    assert len(played) == expected
