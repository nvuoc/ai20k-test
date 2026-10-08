"""Opt-in cloud smoke: dispatch worker, hear Azure greeting, create no booking.

Run from backend: python examples/smoke_voice_live.py --live
Uses configured LiveKit/Azure credentials and a separate fixture API database.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


async def smoke():
    from livekit import rtc

    from app.config import Settings

    settings = Settings.load()
    if not all((settings.livekit_url, settings.livekit_api_key, settings.livekit_api_secret,
                settings.voice_agent_secret, os.getenv("AZURE_SPEECH_KEY"),
                os.getenv("AZURE_SPEECH_REGION"))):
        raise RuntimeError("LiveKit/Azure/voice configuration is incomplete")
    workspace = BACKEND.parents[1]
    cache = workspace / ".cache"
    cache.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="voice-smoke-", dir=cache) as temporary:
        env = {**os.environ, "APP_PROFILE": "fixture_demo", "VOICE_ENABLED": "true",
               "BUSINESS_DB_PATH": str(Path(temporary) / "app.sqlite"),
               "CHECKPOINT_DB_PATH": str(Path(temporary) / "checkpoints.sqlite"),
               "VOICE_API_URL": "http://127.0.0.1:8002",
               "ALLOWED_ORIGINS": "http://127.0.0.1:8002"}
        children = []
        logs = []
        room = rtc.Room()
        tracks = []
        heard = asyncio.Event()

        async def receive_audio(track):
            audio = rtc.AudioStream(track)
            try:
                async for event in audio:
                    if event.frame.samples_per_channel > 0:
                        heard.set()
                        return
            finally:
                await audio.aclose()

        @room.on("track_subscribed")
        def subscribed(track, publication, participant):
            if track.kind == rtc.TrackKind.KIND_AUDIO:
                tracks.append(asyncio.create_task(receive_audio(track)))

        try:
            for index, arguments in enumerate((["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                               "--port", "8002", "--no-access-log"],
                              ["-m", "app.voice.agent", "start"])):
                log = (cache / f"voice-smoke-{index}.log").open("w", encoding="utf-8")
                logs.append(log)
                children.append(subprocess.Popen([sys.executable, *arguments], cwd=BACKEND,
                    env=env, stdout=log, stderr=log,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
            async with httpx.AsyncClient(base_url=env["VOICE_API_URL"], timeout=10) as client:
                for _ in range(100):
                    try:
                        if (await client.get("/api/ready")).status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    if any(child.poll() is not None for child in children):
                        raise RuntimeError("API or voice worker exited before readiness")
                    await asyncio.sleep(0.3)
                (await client.get("/api/bootstrap")).raise_for_status()
                result = await client.post("/api/sessions", json={
                    "client_session_key": "live-voice-smoke", "customer_name": "Voice smoke test",
                    "customer_phone": "0901234567"})
                result.raise_for_status()
                sid = result.json()["session_id"]
                result = await client.post(f"/api/sessions/{sid}/voice")
                result.raise_for_status()
                credentials = result.json()
                print("Connecting cloud test room...", flush=True)
                await asyncio.wait_for(room.connect(credentials["server_url"],
                                                   credentials["participant_token"]), timeout=30)
                print("Cloud connected; waiting for worker greeting...", flush=True)
                await asyncio.wait_for(heard.wait(), timeout=90)
                await asyncio.sleep(2)
                # Wait until greeting playout completes before publishing a test utterance.
                for _ in range(100):
                    if any(p.attributes.get("lk.agent.state") == "listening"
                           for p in room.remote_participants.values()):
                        break
                    await asyncio.sleep(0.1)
                async with httpx.AsyncClient(timeout=20) as speech_client:
                    speech = await speech_client.post(
                        "https://" + os.environ["AZURE_SPEECH_REGION"]
                        + ".tts.speech.microsoft.com/cognitiveservices/v1",
                        headers={"Ocp-Apim-Subscription-Key": os.environ["AZURE_SPEECH_KEY"],
                                 "Content-Type": "application/ssml+xml",
                                 "X-Microsoft-OutputFormat": "raw-24khz-16bit-mono-pcm"},
                        content=('<speak version="1.0" xml:lang="vi-VN">'
                                 '<voice name="vi-VN-HoaiMyNeural">'
                                 'Bạn có những loại xe nào?</voice></speak>').encode("utf-8"))
                    speech.raise_for_status()
                source = rtc.AudioSource(24000, 1)
                track = rtc.LocalAudioTrack.create_audio_track("voice-smoke-microphone", source)
                await room.local_participant.publish_track(track, rtc.TrackPublishOptions(
                    source=rtc.TrackSource.SOURCE_MICROPHONE))
                await asyncio.sleep(1)
                pcm = bytes(24000 * 2) + speech.content + bytes(24000 * 4)
                for offset in range(0, len(pcm), 960):
                    chunk = pcm[offset:offset + 960].ljust(960, b"\0")
                    await source.capture_frame(rtc.AudioFrame(chunk, 24000, 1, 480))
                await source.wait_for_playout()
                for _ in range(100):
                    snapshot = (await client.get(f"/api/sessions/{sid}")).json()
                    user_events = [event for event in snapshot["events"] if event["type"] == "message_received"]
                    assistant_events = [event for event in snapshot["events"] if event["type"] == "assistant_response"]
                    if user_events and len(assistant_events) >= 2 and not snapshot["pending_count"]:
                        break
                    await asyncio.sleep(0.3)
                else:
                    print("Speech events received:", len(user_events), "assistant events:", len(assistant_events), flush=True)
                    raise RuntimeError("Azure STT or shared booking-core response did not arrive")
                await source.aclose()
                snapshot = (await client.get(f"/api/sessions/{sid}")).json()
                assert snapshot["booking"] is None
                print("PASS: LiveKit dispatch, Azure TTS audio, Azure STT and shared core; no booking created.")
        finally:
            await room.disconnect()
            for task in tracks:
                task.cancel()
            await asyncio.gather(*tracks, return_exceptions=True)
            for child in reversed(children):
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                elif child.poll() is None:
                    child.terminate()
                child.wait(timeout=15)
            for log in logs:
                log.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.parse_args()
    asyncio.run(smoke())
