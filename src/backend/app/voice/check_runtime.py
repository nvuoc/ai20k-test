"""Offline image check: native libraries, bundled VAD and speech configuration."""

import asyncio
import os

import azure.cognitiveservices.speech as speechsdk
from livekit.plugins import silero

from app.voice.agent import speech_plugins


async def check() -> None:
    # Dummy credentials; no connection or synthesis is attempted.
    os.environ["AZURE_SPEECH_KEY"] = "offline-ci-key"
    os.environ["AZURE_SPEECH_REGION"] = "southeastasia"
    speechsdk.SpeechConfig(subscription="offline-ci-key", region="southeastasia")
    stt, tts = speech_plugins()
    await asyncio.to_thread(silero.VAD.load)
    await stt.aclose()
    await tts.aclose()
    print("Voice runtime passed: Azure native SDK, STT/TTS plugins, bundled Silero model.")


if __name__ == "__main__":
    asyncio.run(check())
