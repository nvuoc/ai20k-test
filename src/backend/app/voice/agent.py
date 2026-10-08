"""Run: python -m app.voice.agent dev (or start in production).

Azure STT -> existing booking/LLM core over HTTP -> Azure TTS over LiveKit.
No booking databases or model credentials are needed in this worker.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

from dotenv import load_dotenv
from livekit import agents
from livekit.agents import Agent, AgentSession, StopResponse, room_io
from livekit.plugins import azure, silero

from app.config import ROOT
from app.voice.bridge import BookingBridge

logger = logging.getLogger(__name__)
if __name__ == "__main__":
    load_dotenv(ROOT / ".env", override=False)
server = agents.AgentServer(num_idle_processes=1)


def speech_plugins():
    # AZURE_SPEECH_ENDPOINT may be a generic Cognitive Services base URL.
    # TTS requires the synthesis endpoint, not that base URL.
    endpoint = os.getenv("AZURE_TTS_ENDPOINT") or (
        "https://" + os.environ["AZURE_SPEECH_REGION"] + ".tts.speech.microsoft.com/cognitiveservices/v1")
    return (azure.STT(language="vi-VN"), azure.TTS(
        language="vi-VN", voice=os.getenv("AZURE_SPEECH_VOICE", "vi-VN-HoaiMyNeural"),
        speech_endpoint=endpoint))


class BookingAgent(Agent):
    def __init__(self, bridge: BookingBridge):
        super().__init__(instructions="Trợ lý đặt xe tiếng Việt. Lõi ParrotGo quản lý hội thoại.")
        self.bridge = bridge
        self.lock = asyncio.Lock()
        self.audio_failed = False

    async def speak(self, response: dict):
        self.audio_failed = False
        speech = self.session.say(response["text"], allow_interruptions=False)
        await speech.wait_for_playout()
        if not speech.interrupted and not self.audio_failed:
            await self.bridge.played(response)

    async def on_enter(self):
        async with self.lock:
            view = await self.bridge.resume()
            # Resume current response, never infer consent from an old transcript.
            if view.get("active_response"):
                await self.speak(view["active_response"])
            else:
                await self.session.say("Xin chào! Bạn muốn được đón ở đâu và đi đến đâu?",
                                       allow_interruptions=False).wait_for_playout()

    async def on_user_turn_completed(self, turn_ctx, new_message):
        text = new_message.text_content
        if not text or not text.strip():
            raise StopResponse()
        logger.info("Voice utterance sent to booking core")
        async with self.lock:
            try:
                response = await self.bridge.ask(text)
                await self.speak(response)
            except Exception:
                # Do not log credentials, HTTP URLs, customer speech or profiles.
                logger.warning("Voice turn failed; durable input preserved")
                await self.session.say(
                    "Kết nối đang gặp lỗi. Bạn hãy kết nối lại; thông tin chuyến vẫn được giữ.",
                    allow_interruptions=False).wait_for_playout()
        raise StopResponse()


@server.rtc_session(agent_name=os.getenv("LIVEKIT_AGENT_NAME", "parrotgo-booking"))
async def entrypoint(ctx: agents.JobContext):
    metadata = json.loads(ctx.job.metadata)
    if metadata["room"] != ctx.job.room.name:
        raise ValueError("Voice room metadata mismatch")
    bridge = BookingBridge(os.environ["VOICE_API_URL"], os.environ["VOICE_AGENT_SECRET"],
                           metadata["session_id"], metadata["room"])
    ctx.add_shutdown_callback(bridge.close)
    agent = BookingAgent(bridge)
    stt, tts = speech_plugins()
    session = AgentSession(
        stt=stt,
        tts=tts,
        vad=await asyncio.to_thread(silero.VAD.load),
        turn_handling={"turn_detection": "vad", "interruption": {"enabled": False}},
        preemptive_generation=False,
    )

    @session.on("error")
    def on_error(event):
        agent.audio_failed = True

    @session.on("user_input_transcribed")
    def on_transcribed(event):
        if event.is_final:
            logger.info("Final speech transcription received")

    await ctx.connect()
    await session.start(agent=agent, room=ctx.room,
                        room_options=room_io.RoomOptions(participant_identity=metadata["identity"],
                                                        text_input=False))


if __name__ == "__main__":
    agents.cli.run_app(server)
