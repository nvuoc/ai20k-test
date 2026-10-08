"""Verify the P-016 ZIP, existing-file preservation and exported runtime."""

import asyncio
import hashlib
import json
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path

workspace = Path(__file__).resolve().parents[1]
archive_path = workspace / "exports/parrotgo-p016-source.zip"
host = workspace / ("exports/p016-existing-host-check-" + uuid.uuid4().hex[:8])
host.mkdir(exist_ok=False)
existing = {}
for name in (
    "src/main.py", "src/config.py", "src/modules/core/api.py",
    "src/modules/llm/api.py", "src/modules/tool/api.py",
    "src/modules/speech/api.py", "src/api/voice_routes.py", "worker/agent.py",
    "frontend/index.html", "README.md", "requirements.txt", "Dockerfile",
    ".env", "data/rides.db",
    "src/__init__.py", "src/api/__init__.py", "src/domain/__init__.py",
    "src/models/__init__.py", "src/services/__init__.py",
    "src/modules/__init__.py", "src/modules/core/__init__.py",
    "src/modules/llm/__init__.py", "src/modules/tool/__init__.py",
):
    path = host / name
    path.parent.mkdir(parents=True, exist_ok=True)
    value = b"# Existing P-016 file\n"
    if name == ".env":
        value = b"APP_PROFILE=existing_host_value\n"
    path.write_bytes(value)
    existing[name] = value

with zipfile.ZipFile(archive_path) as archive:
    assert archive.testzip() is None
    names = archive.namelist()
    assert len(names) == len(set(names))
    assert not set(names).intersection(existing)
    assert all(not name.startswith(("/", "P-016/", "parrotgo/"))
               and ".." not in Path(name).parts for name in names)
    assert all(Path(name).suffix not in {".sqlite", ".db", ".pyc", ".pyo"}
               and Path(name).name != ".app_secret" for name in names)
    assert not any(set(Path(name).parts).intersection({"node_modules", ".venv", ".git", "__pycache__"}) for name in names)
    archive.extractall(host)

for name, value in existing.items():
    assert (host / name).read_bytes() == value, name
manifest = json.loads((host / "docs/PARROTGO_CHAT_MANIFEST.json").read_text(encoding="utf-8"))
assert {item["path"] for item in manifest["files"]} == set(names) - {"docs/PARROTGO_CHAT_MANIFEST.json"}
for record in manifest["files"]:
    assert hashlib.sha256((host / record["path"]).read_bytes()).hexdigest() == record["sha256"]

subprocess.run([sys.executable, str(host / "scripts/parrotgo_chat/init_env.py")], check=True, capture_output=True)
env_content = (host / ".env.parrotgo-chat").read_bytes()
subprocess.run([sys.executable, str(host / "scripts/parrotgo_chat/init_env.py")], check=True, capture_output=True)
assert (host / ".env.parrotgo-chat").read_bytes() == env_content
assert (host / ".env").read_bytes() == existing[".env"]
subprocess.run([sys.executable, str(host / "run_parrotgo_chat.py"), "--help"],
               cwd=host, check=True, capture_output=True)
sys.path.insert(0, str(host))
from fastapi.testclient import TestClient
from src.api.parrotgo_chat.routes import create_app
from src.modules.core.parrotgo_chat.api import core_func
from src.services.parrotgo_chat.config import ROOT, Settings

assert ROOT == host
loaded = Settings.load()
assert loaded.profile == "fixture_demo"
assert loaded.database_path == host / "data/parrotgo_chat/app.sqlite"
config = Settings(profile="test", secret="bundle-smoke-test",
                  database_path=host / ".smoke/http.sqlite",
                  checkpoint_path=host / ".smoke/http-graph.sqlite")
with TestClient(create_app(config)) as client:
    assert client.get("/api/ready").status_code == 200
    home = client.get("/")
    assert home.status_code == 200 and "text/html" in home.headers["content-type"]
    assert client.get("/api/bootstrap").status_code == 200
    response = client.post("/api/sessions", json={
        "client_session_key": "bundle-check", "customer_phone": "0901234567", "customer_name": "An",
    })
    assert response.status_code == 200, response.text
    for asset in (host / "frontend/parrotgo_chat/dist/assets").iterdir():
        assert client.get("/assets/" + asset.name).status_code == 200

text_config = Settings(profile="test", secret="bundle-text-test",
                       database_path=host / ".smoke/text.sqlite",
                       checkpoint_path=host / ".smoke/text-graph.sqlite")
reply = asyncio.run(core_func("Bạn là ai?", "bundle-text", settings=text_config,
                             customer_phone="0901234567", customer_name="An"))
assert isinstance(reply, str) and reply.strip()
for name, value in existing.items():
    assert (host / name).read_bytes() == value, name
print(json.dumps({"files": len(names), "manifest_hashes": "passed", "zip_crc": "passed",
                  "host_files_preserved": len(existing), "isolated_env": "passed",
                  "http_ui_and_assets": "passed", "core_func": "passed"}))
