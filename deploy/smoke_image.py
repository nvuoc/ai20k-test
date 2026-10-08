"""Exercise a built Docker image without real model/maps keys or booking providers."""

from __future__ import annotations

import http.cookiejar
import json
import subprocess
import time
import urllib.error
import urllib.request
import uuid


def docker(*args: str) -> str:
    return subprocess.check_output(["docker", *args], text=True).strip()


def main() -> None:
    suffix = uuid.uuid4().hex[:12]
    container = f"parrotgo-ci-{suffix}"
    volume = f"parrotgo-ci-data-{suffix}"
    client = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    base = ""

    def request(path: str, body: dict | None = None):
        req = urllib.request.Request(
            base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"} if body is not None else {},
        )
        with client.open(req, timeout=10) as response:
            payload = response.read()
            return json.loads(payload) if "application/json" in response.headers.get("Content-Type", "") else payload

    def wait_ready() -> None:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                if request("/api/ready")["status"] == "ready":
                    return
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                pass
            time.sleep(1)
        raise RuntimeError("Image did not become ready")

    def server_url() -> str:
        address = docker("port", container, "8000/tcp").splitlines()[0]
        if not address.startswith("127.0.0.1:"):
            raise RuntimeError("Smoke-test port must be loopback only")
        return "http://" + address

    try:
        docker("volume", "create", volume)
        docker(
            "run", "--detach", "--name", container, "--read-only",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--mount", f"type=volume,src={volume},dst=/app/backend/data",
            "--publish", "127.0.0.1::8000",
            "--env", "APP_PROFILE=fixture_demo",
            "--env", "APP_SECRET=ci-fixture-signing-secret-only-not-for-production",
            "--env", "COOKIE_SECURE=false",
            "--env", "VOICE_ENABLED=false",
            "--env", "LOCATION_CONFIRMATION_ENABLED=true",
            "--env", "WEATHER_PROVIDER=disabled", "parrotgo:local",
        )
        base = server_url()
        wait_ready()
        page = request("/")
        assert isinstance(page, bytes) and b'<div id="root"' in page
        asset = page.decode().split('src="/assets/', 1)[1].split('"', 1)[0]
        assert request("/assets/" + asset), "Frontend JavaScript is missing"
        config = request("/api/bootstrap")
        assert config["profile"] == "fixture_demo" and config["booking_provider"] == "sandbox"
        session = request("/api/sessions", {
            "client_session_key": "ci-" + suffix,
            "customer_name": "CI fixture", "customer_phone": "0901234567",
        })
        path = "/api/sessions/" + session["session_id"]
        welcome_id = session["active_response"]["response_id"]
        message = {"client_message_id": "ci-greeting", "text": "xin chào"}
        receipt = request(path + "/messages", message)
        assert request(path + "/messages", message)["event_id"] == receipt["event_id"]
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            snapshot = request(path)
            response = snapshot.get("active_response") or {}
            if response.get("response_id") and response["response_id"] != welcome_id:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("Offline worker did not publish a reply")
        response_id = response["response_id"]
        docker("restart", container)
        base = server_url()
        wait_ready()
        restored = request(path)
        assert restored["active_response"]["response_id"] == response_id
        assert request(path + "/messages", message)["event_id"] == receipt["event_id"]
        print("Image smoke passed: frontend/assets, offline reply, idempotency, SQLite restart.")
    except BaseException:
        # Only offline fixture data and a public CI secret are used by this container.
        subprocess.run(["docker", "logs", "--tail", "80", container], check=False)
        raise
    finally:
        # Delete only this run's randomly named fixture container and volume.
        subprocess.run(["docker", "rm", "--force", container], check=False, stdout=subprocess.DEVNULL)
        subprocess.run(["docker", "volume", "rm", volume], check=False, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
