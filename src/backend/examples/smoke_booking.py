"""Exercise real Gemini/VietMap through HTTP handlers; create/cancel only sandbox.

Uses public sample addresses and a synthetic phone, isolated business/checkpoint
databases, and the same persisted Gemini quota as the normal application.
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from dataclasses import replace

from fastapi.testclient import TestClient

from app.config import ROOT, Settings
from app.main import create_app


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    settings = Settings.load()
    directory = ROOT.parent / ".cache/live-booking" / uuid.uuid4().hex
    directory.mkdir(parents=True)
    settings = replace(settings, profile="chat_sandbox", maps_provider="vietmap",
                       database_path=directory / "app.sqlite",
                       checkpoint_path=directory / "checkpoints.sqlite")
    report = {"kind": "live-smoke", "model": settings.model, "maps": "vietmap",
              "booking_provider": "sandbox", "steps": [], "passed": False}
    started = time.monotonic()
    try:
        with TestClient(create_app(settings)) as client:
            client.get("/api/bootstrap").raise_for_status()
            snapshot = client.post("/api/sessions", json={
                "client_session_key": uuid.uuid4().hex,
            }).json()
            session_id = snapshot["session_id"]

            def submit(kind, content):
                active = snapshot["active_response"]
                body = {"reply_to_response_id": active["response_id"],
                        "rendered_response_ids": [active["response_id"]]}
                if kind == "messages":
                    body.update(client_message_id=uuid.uuid4().hex, text=content)
                else:
                    body.update(client_action_id=uuid.uuid4().hex, action=content)
                receipt = client.post(f"/api/sessions/{session_id}/{kind}", json=body)
                receipt.raise_for_status()
                event_id = receipt.json()["event_id"]
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    view = client.get(f"/api/sessions/{session_id}").json()
                    if any(e["event_id"] == event_id + ":failed" for e in view["events"]):
                        raise RuntimeError("TURN_FAILED")
                    if any(e["event_id"] == event_id + ":response" for e in view["events"]):
                        report["steps"].append({"kind": kind,
                            "status": view["booking_status"],
                            "action": view["active_response"]["action"],
                            "reason": view["active_response"].get("reason")})
                        return view
                    time.sleep(0.1)
                raise RuntimeError("TURN_TIMEOUT")

            snapshot = submit("messages", "Đón tôi ở Nhà hát Lớn Hà Nội, đến Ga Hà Nội, "
                              "đi ngay, 2 người, xe 4 chỗ, số điện thoại 0901234567.")
            for _ in range(6):
                response = snapshot["active_response"]
                if response["action"] != "offer_candidates":
                    break
                candidate = response["candidates"][0]
                snapshot = submit("actions", {"type": "select_candidate",
                    "candidate_set_id": candidate["candidate_set_id"],
                    "candidate_id": candidate["candidate_id"]})
            if snapshot["booking_status"] != "awaiting_confirmation":
                raise RuntimeError("NO_CURRENT_SUMMARY")
            summary = snapshot["active_response"]["summary"]
            snapshot = submit("actions", {"type": "confirm_booking",
                "prompt_id": summary["prompt_id"], "booking_revision": summary["booking_revision"],
                "snapshot_fingerprint": summary["snapshot_fingerprint"]})
            if snapshot["booking_status"] != "booked":
                raise RuntimeError("BOOKING_NOT_CONFIRMED")
            booking_id = snapshot["booking"]["booking_id"]
            restored = client.get(f"/api/sessions/{session_id}").json()
            if restored["booking"]["booking_id"] != booking_id:
                raise RuntimeError("RESTORE_MISMATCH")
            snapshot = submit("actions", {"type": "cancel_booking", "booking_id": booking_id})
            if snapshot["booking_status"] != "cancelled":
                raise RuntimeError("CANCELLATION_NOT_CONFIRMED")
            report.update(passed=True, booking_id=booking_id,
                          natural_turns=1, final_status="cancelled")
    except Exception as exc:
        # Keep provider error bodies and key-bearing URLs out of diagnostics.
        report["error_type"] = type(exc).__name__
        failed_response = getattr(exc, "response", None)
        if failed_response is not None:
            report["http_status"] = failed_response.status_code
            report["error_code"] = failed_response.json().get("code", "HTTP_ERROR")
        if isinstance(exc, RuntimeError):
            report["error_code"] = str(exc)
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    destination = ROOT / "evaluation/live-booking-report.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
