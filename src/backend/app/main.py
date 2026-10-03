"""FastAPI chat server with durable inbox, safe projections and same-origin UI."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api_store import ApiStore, Conflict
from app.config import ROOT, Settings
from app.contracts.chat import (
    AckInput,
    ActionInput,
    BookingResponse,
    Bootstrap,
    ChatSnapshot,
    DeliveryAck,
    HealthResponse,
    HttpError,
    MessageInput,
    PublicError,
    ReadyResponse,
    Receipt,
    SessionInput,
)
from app.contracts.weather import WeatherFact, WeatherRequest
from app.workers.coordinator import Coordinator


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        from app.runtime import conversation_runtime

        config = settings or Settings.load()
        app.state.config = config
        app.state.creation_locks = {}
        store = ApiStore(config.database_path)
        app.state.store = store
        async with conversation_runtime(config) as (engine, graph):
            app.state.engine = engine
            app.state.graph = graph
            coordinator = Coordinator(store, graph)
            app.state.coordinator = coordinator
            runner = asyncio.create_task(coordinator.run())
            try:
                yield
            finally:
                await coordinator.close()
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)

    def public_config():
        value = app.state.config.public()
        engine = app.state.engine
        value["capabilities"].update(location_confirmation=engine.location_confirmation, area_estimate=engine.location_confirmation, area_assistance=engine.area_assistance_enabled, text_only_chat=True)
        value["capabilities"]["electric_motorbike"] = bool(engine.vehicle_catalog.get("xe_may_dien", {}).get("bookable"))
        value["capabilities"]["weather"] = engine.inquiry_service.weather is not None
        value["vehicles"] = [{"code":code, "label":row["label"], "max_passengers":row["max_passengers"]}
            for code, row in engine.vehicle_catalog.items() if row.get("bookable") and code in {"oto_4_cho", "oto_7_cho", "xe_may_dien"}]
        return value

    app = FastAPI(title="ParrotGo · Chat đặt xe thử nghiệm", version="2.0.0", lifespan=lifespan,
                  responses={401:{"model":HttpError}, 404:{"model":HttpError},
                             403:{"model":PublicError}, 409:{"model":PublicError},
                             422:{"model":PublicError}})

    @app.middleware("http")
    async def origin_guard(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            config = getattr(app.state, "config", settings)
            if config and origin and origin not in config.allowed_origins:
                return JSONResponse({"code": "ORIGIN_NOT_ALLOWED", "message": "Origin không hợp lệ.",
                                     "retryable": False}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(Conflict)
    async def conflict_handler(request, error):
        message = ("Phiên gặp lỗi xử lý kéo dài và cần được kiểm tra. Các thao tác đặt xe đã dừng; "
                   "kết quả giao dịch chưa rõ vẫn được lưu để đối soát."
                   if str(error) == "SESSION_NEEDS_SUPPORT" else
                   "Tham chiếu cũ hoặc ID đã dùng với nội dung khác.")
        return JSONResponse({"code": str(error), "message": message,
                             "retryable": False}, status_code=409)

    @app.exception_handler(RequestValidationError)
    async def invalid_handler(request, error):
        # Pydantic's raw input can include private chat/contact data.
        return JSONResponse({"code": "INPUT_INVALID", "message": "Dữ liệu không hợp lệ hoặc quá dài.",
                             "retryable": False}, status_code=422)

    def signed_owner(value):
        signature = hmac.new(app.state.config.secret.encode(), value.encode(), hashlib.sha256).hexdigest()
        return value + "." + signature

    def owner(request):
        cookie = request.cookies.get("chat_owner", "")
        value = cookie.split(".")[0]
        if value and hmac.compare_digest(cookie, signed_owner(value)):
            return value
        raise HTTPException(401, "Hãy khởi tạo phiên trình duyệt qua /api/bootstrap.")

    def owned(request, session_id):
        if not app.state.store.owned(session_id, owner(request)):
            raise HTTPException(404, "Không tìm thấy phiên.")

    def safe_booking(value):
        if not value:
            return None
        return {key: value.get(key) for key in
                ("booking_id", "status", "provider_status", "provider")}

    def safe_response(value):
        if not value:
            return None
        result = {key: value.get(key) for key in
                  ("response_id", "generation", "text", "action", "focus", "candidates",
                   "summary", "presentation", "booking_status", "reason", "inquiry")}
        result["booking"] = safe_booking(value.get("booking"))
        return result

    def projection(snapshot):
        state = snapshot.pop("state")
        # Only approved chat facts and presentation refs cross the API boundary.
        snapshot.update({"api_version": "chat-api-3" if state.get("control", {}).get("schema_version", 4) >= 6 else "chat-api-2", "booking_status": state.get("booking_status"),
                         "booking": safe_booking(state.get("transaction", {}).get("booking_result")),
                         "draft_id": state.get("control", {}).get("draft_id"),
                         "active_response": safe_response(state.get("last_response"))})
        for event in snapshot["events"]:
            if event["type"] == "assistant_response":
                event["payload"] = safe_response(event["payload"])
            elif event["type"] == "booking_updated":
                event["payload"] = safe_booking(event["payload"])
        return snapshot

    def public_receipt(receipt):
        state = app.state.store.snapshot(receipt["session_id"], limit=1)["state"]
        receipt["api_version"] = "chat-api-3" if state["control"].get("schema_version", 4) >= 6 else "chat-api-2"
        return receipt

    @app.get("/api/health", response_model=HealthResponse)
    async def health():
        return {"status": "ok"}

    @app.get("/api/ready", response_model=ReadyResponse)
    async def ready():
        return {"status": "ready", **public_config()}

    @app.get("/api/bootstrap", response_model=Bootstrap)
    async def bootstrap(request: Request):
        try:
            owner(request)
            value = None
        except HTTPException:
            value = secrets.token_urlsafe(24)
        response = JSONResponse(Bootstrap.model_validate(public_config()).model_dump(mode="json"))
        if value:
            response.set_cookie("chat_owner", signed_owner(value), httponly=True,
                                secure=app.state.config.cookie_secure, samesite="strict",
                                max_age=60 * 60 * 24 * 30)
        return response

    @app.post("/api/sessions", response_model=ChatSnapshot)
    async def sessions(request: Request, body: SessionInput):
        owner_id = owner(request)
        lock_key = (owner_id, body.client_session_key)
        lock = app.state.creation_locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            session_id = app.state.store.find_session(owner_id, body.client_session_key)
            if not session_id:
                session_id = "session_" + uuid.uuid4().hex
                state = app.state.engine.new_state(session_id)
                await app.state.graph.initialize(session_id, state)
                app.state.store.create_session(session_id, owner_id, body.client_session_key, state)
        return projection(app.state.store.snapshot(session_id))

    @app.get("/api/sessions/{session_id}", response_model=ChatSnapshot)
    async def session(request: Request, session_id: str, after_cursor: int = Query(0, ge=0),
                      limit: int = Query(100, ge=1, le=200)):
        owned(request, session_id)
        return projection(app.state.store.snapshot(session_id, after=after_cursor, limit=limit))

    @app.get("/api/sessions/{session_id}/updates", response_model=ChatSnapshot)
    async def updates(request: Request, session_id: str, after_cursor: int = Query(0, ge=0),
                      limit: int = Query(50, ge=1, le=200)):
        owned(request, session_id)
        return projection(app.state.store.snapshot(session_id, after=after_cursor, limit=limit))

    @app.post("/api/sessions/{session_id}/messages", status_code=202, response_model=Receipt)
    async def messages(request: Request, session_id: str, body: MessageInput):
        owned(request, session_id)
        result = app.state.store.enqueue(session_id, "message", body.client_message_id,
                                         body.model_dump())
        app.state.coordinator.wakeup.set()
        return public_receipt(result)

    @app.post("/api/sessions/{session_id}/actions", status_code=202, response_model=Receipt)
    async def actions(request: Request, session_id: str, body: ActionInput):
        owned(request, session_id)
        result = app.state.store.enqueue(session_id, "action", body.client_action_id,
                                         body.model_dump())
        app.state.coordinator.wakeup.set()
        return public_receipt(result)

    @app.post("/api/sessions/{session_id}/delivery-acks", response_model=DeliveryAck)
    async def ack(request: Request, session_id: str, body: AckInput):
        owned(request, session_id)
        app.state.store.ack(session_id, body.model_dump())
        return {"status": "acknowledged"}

    @app.get("/api/sessions/{session_id}/booking", response_model=BookingResponse)
    async def booking(request: Request, session_id: str):
        owned(request, session_id)
        view = projection(app.state.store.snapshot(session_id, limit=1))
        return {"booking": view["booking"], "status": view["booking_status"]}

    @app.get("/api/sessions/{session_id}/weather", response_model=WeatherFact)
    async def weather(request: Request, session_id: str, at: datetime,
                      target: Literal["pickup", "destination"] = "pickup",
                      inquiry_id: str | None = None):
        """Read a forecast for a sourced session location, without changing a draft."""
        from app.domain.engine import fingerprint
        owned(request, session_id)
        adapter = app.state.engine.inquiry_service.weather
        if adapter is None:
            raise HTTPException(503, "Dịch vụ thời tiết chưa được cấu hình.")
        if at.tzinfo is None:
            raise HTTPException(422, "Thời điểm cần có múi giờ, ví dụ 2026-10-03T08:00:00+07:00.")
        state = app.state.store.snapshot(session_id)["state"]
        if inquiry_id:
            item = state.get("inquiries", {}).get(inquiry_id)
            if not item or item["expires_at"] <= app.state.engine.clock():
                raise HTTPException(404, "Không tìm thấy tuyến hỏi thử còn hiệu lực.")
            place = item["locations"].get("origin" if target == "pickup" else "destination", {}).get("place")
            scope_kind, scope_id = "inquiry", inquiry_id
        else:
            committed = state["transaction"].get("committed_snapshot")
            place = committed.get(target) if committed else state["resolution"]["locations"].get(target, {}).get("place")
            scope_kind, scope_id = "booking", state["control"]["draft_id"]
        if not place:
            raise HTTPException(409, "Địa điểm chưa được xác định; hãy bổ sung địa chỉ trước.")
        dep = fingerprint([session_id, scope_kind, scope_id, place, at.isoformat()])
        return await adapter.forecast(WeatherRequest(request_id="weather_"+dep[:24],
            scope_kind=scope_kind, scope_id=scope_id, location_ref=place["id"],
            latitude=place["lat"], longitude=place["lon"], target_time=at, dependency_fingerprint=dep))

    dist = ROOT.parent / "frontend/dist"
    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    async def home():
        if (dist / "index.html").exists():
            return FileResponse(dist / "index.html")
        return JSONResponse({"message": "Chạy npm --prefix src/frontend run build hoặc mở Vite cổng 5173."})

    return app


app = create_app()
