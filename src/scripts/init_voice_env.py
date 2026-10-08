"""Add missing voice bridge settings without printing or replacing credentials."""
import secrets
from pathlib import Path

target = Path(__file__).resolve().parents[1] / "backend/.env"
if not target.exists():
    raise SystemExit("Run init_env.py first")
content = target.read_text(encoding="utf-8")
values = dict(line.split("=", 1) for line in content.splitlines()
              if "=" in line and not line.lstrip().startswith("#"))
defaults = {"VOICE_ENABLED": "true", "VOICE_API_URL": "http://127.0.0.1:8000",
            "LIVEKIT_AGENT_NAME": "parrotgo-booking", "AZURE_SPEECH_VOICE": "vi-VN-HoaiMyNeural"}
missing = [f"{key}={value}" for key, value in defaults.items() if key not in values]
if not values.get("VOICE_AGENT_SECRET", "").strip():
    content = "\n".join(line for line in content.splitlines()
                        if not line.startswith("VOICE_AGENT_SECRET="))
    missing.append("VOICE_AGENT_SECRET=" + secrets.token_urlsafe(48))
if missing:
    target.write_text(content.rstrip() + "\n\n# Voice bridge settings\n" + "\n".join(missing) + "\n",
                      encoding="utf-8")
print("Voice bridge configured; existing provider credentials preserved.")
