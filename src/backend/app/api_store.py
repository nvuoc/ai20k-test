"""Durable ingress and published API projections, separate from graph checkpoints."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app.config import Settings


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def transaction_unknown(state):
    return (state.get("booking_status") in {"booking_unknown", "cancel_unknown"}
            or (state.get("transaction", {}).get("active_operation") or {}).get("status") in {"dispatched", "unknown"})


class Conflict(Exception):
    pass


class AdmissionError(Exception):
    """Safe rejection before accepting new work; existing receipts stay valid."""

    def __init__(self, code, message, *, retry_after_seconds=None):
        super().__init__(message)
        self.code = code
        self.retry_after_seconds = retry_after_seconds


class ApiStore:
    def __init__(self, path: Path, *, limits: Settings | None = None, clock=None):
        self.path = Path(path)
        self.limits = limits or Settings()
        self.clock = clock or time.time
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS api_sessions (
              id TEXT PRIMARY KEY, owner TEXT NOT NULL, client_key TEXT NOT NULL,
              projection TEXT NOT NULL, latest_seq INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL, UNIQUE(owner,client_key));
            CREATE TABLE IF NOT EXISTS api_inbox (
              id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES api_sessions(id),
              seq INTEGER NOT NULL, kind TEXT NOT NULL, client_key TEXT NOT NULL,
              hash TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
              created_at TEXT NOT NULL, UNIQUE(session_id,kind,client_key),
              UNIQUE(session_id,seq));
            CREATE INDEX IF NOT EXISTS api_inbox_pending ON api_inbox(status,session_id,seq);
            CREATE TABLE IF NOT EXISTS api_events (
              cursor INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
              session_id TEXT NOT NULL REFERENCES api_sessions(id), type TEXT NOT NULL,
              payload TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS api_event_session ON api_events(session_id,cursor);
            CREATE TABLE IF NOT EXISTS api_acks (
              session_id TEXT NOT NULL REFERENCES api_sessions(id), id TEXT NOT NULL,
              response_id TEXT NOT NULL, generation INTEGER NOT NULL, hash TEXT NOT NULL,
              PRIMARY KEY(session_id,id));
            CREATE TABLE IF NOT EXISTS api_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS api_reconciliation (
              session_id TEXT PRIMARY KEY REFERENCES api_sessions(id),
              attempt INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL,
              event_id TEXT, status TEXT NOT NULL DEFAULT 'waiting');
            CREATE TABLE IF NOT EXISTS api_turn_retries (
              event_id TEXT PRIMARY KEY REFERENCES api_inbox(id),
              attempt INTEGER NOT NULL, next_at REAL NOT NULL, status TEXT NOT NULL,
              last_error TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS api_quota_retries (
              event_id TEXT PRIMARY KEY REFERENCES api_inbox(id),
              http_attempt INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS api_admission_requests (
              bucket TEXT NOT NULL, reserved_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS api_admission_bucket_time
              ON api_admission_requests(bucket,reserved_at);
            INSERT OR IGNORE INTO api_meta VALUES('schema','chat-api-2');
            """)

    @contextmanager
    def connection(self, *, write=False):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def find_session(self, owner, client_key):
        with self.connection() as db:
            row = db.execute(
                "SELECT id FROM api_sessions WHERE owner=? AND client_key=?",
                (owner, client_key),
            ).fetchone()
            return row["id"] if row else None

    def create_session(self, session_id, owner, client_key, state):
        with self.connection(write=True) as db:
            existing = db.execute("SELECT id FROM api_sessions WHERE owner=? AND client_key=?", (owner, client_key)).fetchone()
            if existing:
                return existing["id"]
            count = db.execute("SELECT COUNT(*) FROM api_sessions WHERE owner=?", (owner,)).fetchone()[0]
            if self.limits.admission_limits_enabled and count >= self.limits.max_sessions_per_owner:
                raise AdmissionError("OWNER_SESSION_LIMIT", "Bạn đã đạt giới hạn phiên thử nghiệm. Hãy tiếp tục phiên hiện có hoặc liên hệ người quản trị.")
            count = db.execute("SELECT COUNT(*) FROM api_sessions").fetchone()[0]
            if self.limits.admission_limits_enabled and count >= self.limits.max_sessions_total:
                raise AdmissionError("SESSION_CAPACITY", "Hệ thống thử nghiệm đã đạt giới hạn phiên. Người quản trị cần kiểm tra dung lượng.")
            self._reserve_admission(db, owner)
            db.execute("INSERT INTO api_sessions(id,owner,client_key,projection,created_at) "
                       "VALUES(?,?,?,?,?)", (session_id, owner, client_key, encode(state), now()))
            self._event(db, session_id, f"{session_id}:welcome", "assistant_response",
                        state["last_response"])
            return session_id

    def _reserve_admission(self, db, owner):
        """One atomic rolling window for accepted new sessions/events only."""
        if not self.limits.admission_limits_enabled:
            return
        timestamp = self.clock()
        db.execute("DELETE FROM api_admission_requests WHERE reserved_at<=?", (timestamp-60,))
        buckets = (("global", self.limits.inbound_global_rpm),
                   ("owner:" + hashlib.sha256(owner.encode()).hexdigest(), self.limits.inbound_owner_rpm))
        for bucket, limit in buckets:
            count, oldest = db.execute("SELECT COUNT(*),MIN(reserved_at) FROM api_admission_requests WHERE bucket=?", (bucket,)).fetchone()
            if count >= limit:
                raise AdmissionError("INBOUND_RATE_LIMITED", "Bạn gửi yêu cầu quá nhanh. Hãy chờ một chút rồi thử lại.",
                    retry_after_seconds=max(1, math.ceil(oldest+60-timestamp)))
        db.executemany("INSERT INTO api_admission_requests(bucket,reserved_at) VALUES(?,?)", [(bucket, timestamp) for bucket, _ in buckets])

    def owned(self, session_id, owner):
        with self.connection() as db:
            return bool(db.execute("SELECT 1 FROM api_sessions WHERE id=? AND owner=?",
                                   (session_id, owner)).fetchone())

    def _event(self, db, session_id, event_id, kind, payload):
        db.execute("INSERT OR IGNORE INTO api_events(id,session_id,type,payload,created_at) "
                   "VALUES(?,?,?,?,?)", (event_id, session_id, kind, encode(payload), now()))

    def enqueue(self, session_id, kind, client_key, payload):
        immutable = {k: v for k, v in payload.items() if k != "rendered_response_ids"}
        digest = hashlib.sha256(encode(immutable).encode()).hexdigest()
        with self.connection(write=True) as db:
            old = db.execute("SELECT * FROM api_inbox WHERE session_id=? AND kind=? "
                             "AND client_key=?", (session_id, kind, client_key)).fetchone()
            if old:
                if old["hash"] != digest:
                    raise Conflict("IDEMPOTENCY_CONFLICT")
                self._record_rendered(db, session_id, payload.get("rendered_response_ids", []), old["id"])
                return self._receipt(old)
            blocked = db.execute("SELECT 1 FROM api_turn_retries r JOIN api_inbox i "
                                 "ON i.id=r.event_id WHERE i.session_id=? AND r.status='exhausted' LIMIT 1",
                                 (session_id,)).fetchone()
            if blocked:
                raise Conflict("SESSION_NEEDS_SUPPORT")
            row = db.execute("SELECT latest_seq,owner FROM api_sessions WHERE id=?",
                             (session_id,)).fetchone()
            count = db.execute("SELECT COUNT(*) FROM api_inbox WHERE session_id=? AND status IN ('queued','processing')", (session_id,)).fetchone()[0]
            if self.limits.admission_limits_enabled and count >= self.limits.max_pending_per_session:
                raise AdmissionError("SESSION_QUEUE_FULL", "Phiên này đang có nhiều tin nhắn chờ xử lý. Bạn chờ bot trả lời trước khi gửi thêm nhé.", retry_after_seconds=5)
            count = db.execute("SELECT COUNT(*) FROM api_inbox WHERE status IN ('queued','processing')").fetchone()[0]
            if self.limits.admission_limits_enabled and count >= self.limits.max_pending_total:
                raise AdmissionError("INBOX_CAPACITY", "Hệ thống đang bận. Bạn thử gửi lại sau một chút nhé.", retry_after_seconds=5)
            self._reserve_admission(db, row["owner"])
            seq = row["latest_seq"] + 1
            event_id = "evt_" + uuid.uuid4().hex
            self._record_rendered(db, session_id, payload.get("rendered_response_ids", []), event_id)
            db.execute("UPDATE api_sessions SET latest_seq=? WHERE id=?", (seq, session_id))
            db.execute("INSERT INTO api_inbox VALUES(?,?,?,?,?,?,?,?,?)",
                       (event_id, session_id, seq, kind, client_key, digest, encode(payload),
                        "queued", now()))
            self._event(db, session_id, event_id + ":received", "message_received", {
                "message_id": event_id, "event_id": event_id, "role": "user",
                "text": payload.get("text") or self.action_text(payload.get("action", {})),
            })
            return {"api_version": "chat-api-2", "session_id": session_id,
                    "message_id": event_id, "event_id": event_id,
                    "ingress_seq": seq, "status": "received"}

    @staticmethod
    def action_text(action):
        return {"confirm_booking": "Xác nhận đặt xe thử nghiệm", "select_candidate":
                "Chọn địa điểm", "cancel_booking": "Hủy đơn thử nghiệm",
                "cancel_draft": "Hủy yêu cầu đặt xe", "use_inquiry_route": "Dùng tuyến hỏi thử để đặt",
                "choose_inquiry_vehicle": "Xem giá loại xe cho tuyến hỏi thử",
                "resume_booking": "Tiếp tục chuyến đang đặt", "dismiss_inquiry": "Bỏ tuyến hỏi thử"}.get(action.get("type"), "Thao tác")

    @staticmethod
    def _receipt(row):
        return {"api_version": "chat-api-2", "session_id": row["session_id"],
                "message_id": row["id"], "event_id": row["id"],
                "ingress_seq": row["seq"], "status": row["status"]}

    def pending_sessions(self):
        with self.connection() as db:
            return [r[0] for r in db.execute(
                "SELECT DISTINCT i.session_id FROM api_inbox i LEFT JOIN api_turn_retries r "
                "ON r.event_id=i.id WHERE i.status IN ('queued','processing') "
                "AND (r.event_id IS NULL OR (r.status!='exhausted' AND r.next_at<=?)) "
                "AND NOT EXISTS (SELECT 1 FROM api_inbox earlier WHERE earlier.session_id=i.session_id "
                "AND earlier.status IN ('queued','processing') AND earlier.seq<i.seq) "
                "ORDER BY i.created_at,i.id", (time.time(),))]

    def next_pending(self, session_id):
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM api_inbox WHERE session_id=? "
                             "AND status IN ('queued','processing') ORDER BY seq LIMIT 1",
                             (session_id,)).fetchone()
            if not row:
                return None
            retry = db.execute("SELECT status,next_at FROM api_turn_retries WHERE event_id=?",
                               (row["id"],)).fetchone()
            if retry and (retry["status"] == "exhausted" or retry["next_at"] > time.time()):
                return None
            db.execute("UPDATE api_inbox SET status='processing' WHERE id=?", (row["id"],))
            result = dict(row)
            result["payload"] = json.loads(result["payload"])
            return result

    def current_ingress(self, session_id, seq):
        with self.connection() as db:
            row = db.execute("SELECT latest_seq FROM api_sessions WHERE id=?",
                             (session_id,)).fetchone()
            return bool(row and row["latest_seq"] == seq)

    def complete(self, event, state):
        with self.connection(write=True) as db:
            db.execute("UPDATE api_sessions SET projection=? WHERE id=?",
                       (encode(state), event["session_id"]))
            self._event(db, event["session_id"], event["id"] + ":response",
                        "assistant_response", state["last_response"])
            booking = state.get("transaction", {}).get("booking_result")
            if booking:
                self._event(db, event["session_id"], event["id"] + ":booking",
                            "booking_updated", booking)
            db.execute("UPDATE api_inbox SET status='completed' WHERE id=?", (event["id"],))
            db.execute("DELETE FROM api_turn_retries WHERE event_id=?", (event["id"],))
            db.execute("DELETE FROM api_quota_retries WHERE event_id=?", (event["id"],))
            self._sync_reconciliation(db, event["session_id"], state)

    def defer(self, event, code, *, retryable=True):
        """A pending graph execution stays pending and resumes the same event."""
        with self.connection(write=True) as db:
            row = db.execute("SELECT attempt FROM api_turn_retries WHERE event_id=?",
                             (event["id"],)).fetchone()
            attempt = row["attempt"] + 1 if row else 1
            exhausted = not retryable or attempt >= 3
            delay = (1, 2, 5)[min(attempt - 1, 2)]
            db.execute("INSERT INTO api_turn_retries VALUES(?,?,?,?,?) ON CONFLICT(event_id) "
                       "DO UPDATE SET attempt=excluded.attempt,next_at=excluded.next_at,"
                       "status=excluded.status,last_error=excluded.last_error",
                       (event["id"], attempt, time.time() + delay,
                        "exhausted" if exhausted else "waiting", code))
            self._event(db, event["session_id"], event["id"] + f":retry:{attempt}", "turn_failed", {
                "event_id": event["id"], "code": code,
                "retryable": not exhausted, "needs_support": exhausted,
                "message": ("Dịch vụ diễn giải cần kiểm tra cấu hình. Tin nhắn và giao dịch đang dở được giữ lại để hỗ trợ kỹ thuật." if not retryable
                else "Phiên này cần kiểm tra kỹ thuật sau nhiều lần xử lý lỗi. Giao dịch đang dở được giữ để đối soát; hãy mở phiên mới để bắt đầu yêu cầu khác.") if exhausted
                else "Xử lý bị gián đoạn; hệ thống đang tự tiếp tục tin nhắn này. Bạn chưa cần gửi lại.",
            })

    def wait_for_quota(self, event, seconds, *, request_sent=None, max_http_attempts=3):
        """Wait without consuming fault retries; bound repeated remote quota errors.

        Local quota rejection has sent no HTTP and does not consume this separate
        persisted retry counter. Never sleep while holding a worker semaphore.
        """
        with self.connection(write=True) as db:
            previous = db.execute("SELECT status FROM api_turn_retries WHERE event_id=?", (event["id"],)).fetchone()
            if previous and previous["status"] == "exhausted":
                return
            db.execute("INSERT OR IGNORE INTO api_quota_retries(event_id,http_attempt) VALUES(?,0)", (event["id"],))
            if request_sent is not False:
                db.execute("UPDATE api_quota_retries SET http_attempt=http_attempt+1 WHERE event_id=?", (event["id"],))
            count = db.execute("SELECT http_attempt FROM api_quota_retries WHERE event_id=?", (event["id"],)).fetchone()["http_attempt"]
            exhausted = count >= max_http_attempts
            db.execute("INSERT INTO api_turn_retries VALUES(?,?,?,?,?) ON CONFLICT(event_id) "
                "DO UPDATE SET next_at=excluded.next_at,status=excluded.status,last_error=excluded.last_error",
                (event["id"], 0, time.time() + max(1, seconds),
                 "exhausted" if exhausted else "waiting_for_quota", "RATE_LIMITED"))
            db.execute("UPDATE api_inbox SET status='queued' WHERE id=?", (event["id"],))
            if exhausted:
                self._event(db, event["session_id"], event["id"] + ":quota_exhausted", "turn_failed", {
                    "event_id": event["id"], "code": "RATE_LIMITED", "retryable": False,
                    "needs_support": True,
                    "message": "Các dịch vụ diễn giải vẫn từ chối vì hạn mức sau nhiều lần thử. Tin nhắn và giao dịch đang dở được giữ lại để hỗ trợ kỹ thuật.",
                })

    @staticmethod
    def _sync_reconciliation(db, session_id, state):
        if transaction_unknown(state):
            db.execute("INSERT OR IGNORE INTO api_reconciliation(session_id,next_at) VALUES(?,?)",
                       (session_id, time.time() + 1))
        else:
            db.execute("DELETE FROM api_reconciliation WHERE session_id=?", (session_id,))

    def unknown_sessions(self):
        """Find published unknown outcomes and recover their durable schedules."""
        with self.connection(write=True) as db:
            rows = db.execute("SELECT id,projection FROM api_sessions").fetchall()
            unknown = []
            for row in rows:
                state = json.loads(row["projection"])
                if transaction_unknown(state):
                    unknown.append(row["id"])
                    self._sync_reconciliation(db, row["id"], state)
            return unknown

    def due_reconciliations(self):
        self.unknown_sessions()
        with self.connection() as db:
            return [r[0] for r in db.execute(
                "SELECT r.session_id FROM api_reconciliation r WHERE r.status!='exhausted' "
                "AND r.next_at<=? AND NOT EXISTS "
                "(SELECT 1 FROM api_inbox i WHERE i.session_id=r.session_id "
                "AND i.status IN ('queued','processing')) ORDER BY r.next_at LIMIT 20",
                (time.time(),))]

    def claim_reconciliation(self, session_id):
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM api_reconciliation WHERE session_id=?",
                             (session_id,)).fetchone()
            pending = db.execute("SELECT 1 FROM api_inbox WHERE session_id=? "
                                 "AND status IN ('queued','processing') LIMIT 1", (session_id,)).fetchone()
            if not row or pending or row["status"] == "exhausted":
                return None
            if row["next_at"] > time.time():
                return None
            event_id = row["event_id"] or f"reconcile:{session_id}:{row['attempt'] + 1}"
            db.execute("UPDATE api_reconciliation SET status='processing',event_id=? WHERE session_id=?",
                       (event_id, session_id))
            return event_id

    def publish_reconciliation(self, session_id, event_id, state):
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM api_reconciliation WHERE session_id=?",
                             (session_id,)).fetchone()
            if not row or row["event_id"] != event_id:
                return
            db.execute("UPDATE api_sessions SET projection=? WHERE id=?", (encode(state), session_id))
            self._event(db, session_id, event_id + ":response", "assistant_response", state["last_response"])
            booking = state.get("transaction", {}).get("booking_result")
            if booking:
                self._event(db, session_id, event_id + ":booking", "booking_updated", booking)
            if not transaction_unknown(state):
                db.execute("DELETE FROM api_reconciliation WHERE session_id=?", (session_id,))
                return
            attempt = row["attempt"] + 1
            delays = (1, 2, 5, 15, 30)
            db.execute("UPDATE api_reconciliation SET attempt=?,next_at=?,event_id=NULL,status=? "
                       "WHERE session_id=?", (attempt, time.time() + delays[min(attempt, 4)],
                       "exhausted" if attempt >= 5 else "waiting", session_id))

    def reconciliation_failed(self, session_id, event_id):
        """Keep a checkpointed in-progress reconciliation available for resume."""
        with self.connection(write=True) as db:
            db.execute("UPDATE api_reconciliation SET next_at=? WHERE session_id=? AND event_id=?",
                       (time.time() + 5, session_id, event_id))

    def fail(self, event, code):
        with self.connection(write=True) as db:
            self._event(db, event["session_id"], event["id"] + ":failed", "turn_failed", {
                "event_id": event["id"], "code": code, "retryable": True,
                "message": "Chưa xử lý được tin nhắn. Bạn có thể thử lại sau.",
            })
            db.execute("UPDATE api_inbox SET status='failed' WHERE id=?", (event["id"],))

    def ack(self, session_id, payload):
        digest = hashlib.sha256(encode(payload).encode()).hexdigest()
        with self.connection(write=True) as db:
            old = db.execute("SELECT hash FROM api_acks WHERE session_id=? AND id=?",
                             (session_id, payload["ack_id"])).fetchone()
            if old:
                if old[0] != digest:
                    raise Conflict("IDEMPOTENCY_CONFLICT")
                return
            row = db.execute("SELECT projection FROM api_sessions WHERE id=?", (session_id,)).fetchone()
            current = json.loads(row[0]).get("last_response") if row else None
            if not current or current.get("response_id") != payload["response_id"] or current.get("generation") != payload["generation"]:
                raise Conflict("STALE_PRESENTATION")
            db.execute("INSERT INTO api_acks VALUES(?,?,?,?,?)",
                       (session_id, payload["ack_id"], payload["response_id"],
                        payload["generation"], digest))

    def _record_rendered(self, db, session_id, response_ids, ingress_id):
        """Validate client render refs and persist the current ACK with ingress.

        UI may list earlier rendered transcript entries. Those IDs must belong
        to this session, but only the current response can gain new delivery
        evidence. A retry can add current render evidence without another turn.
        """
        if not response_ids:
            return
        row = db.execute("SELECT projection FROM api_sessions WHERE id=?", (session_id,)).fetchone()
        current = json.loads(row[0]).get("last_response") if row else None
        events = db.execute("SELECT payload FROM api_events WHERE session_id=? "
                            "AND type='assistant_response'", (session_id,)).fetchall()
        responses = [json.loads(item[0]) for item in events]
        known = {item["response_id"] for item in responses if isinstance(item, dict) and item.get("response_id")}
        if not set(response_ids).issubset(known):
            raise Conflict("STALE_PRESENTATION")
        if current and current["response_id"] in response_ids:
            ack = {"ack_id": f"ingress:{ingress_id}:{current['response_id']}",
                   "response_id": current["response_id"], "generation": current["generation"],
                   "delivery_type": "rendered"}
            digest = hashlib.sha256(encode(ack).encode()).hexdigest()
            db.execute("INSERT OR IGNORE INTO api_acks VALUES(?,?,?,?,?)",
                       (session_id, ack["ack_id"], ack["response_id"], ack["generation"], digest))

    def delivered(self, session_id):
        with self.connection() as db:
            return [r[0] for r in db.execute("SELECT DISTINCT response_id FROM api_acks "
                                           "WHERE session_id=?", (session_id,))]

    def snapshot(self, session_id, *, after=0, limit=100):
        with self.connection() as db:
            # One read transaction binds history/cursor and the published projection.
            db.execute("BEGIN")
            row = db.execute("SELECT * FROM api_sessions WHERE id=?", (session_id,)).fetchone()
            events = db.execute("SELECT * FROM api_events WHERE session_id=? AND cursor>? "
                                "ORDER BY cursor LIMIT ?", (session_id, after, limit + 1)).fetchall()
            pending = db.execute("SELECT COUNT(*) FROM api_inbox WHERE session_id=? AND "
                                 "status IN ('queued','processing')", (session_id,)).fetchone()[0]
            needs_support = bool(db.execute("SELECT 1 FROM api_turn_retries r JOIN api_inbox i "
                "ON i.id=r.event_id WHERE i.session_id=? AND r.status='exhausted' LIMIT 1",
                (session_id,)).fetchone())
            exhausted_reconciliation = bool(db.execute(
                "SELECT 1 FROM api_reconciliation WHERE session_id=? AND status='exhausted'",
                (session_id,),
            ).fetchone())
            needs_support = needs_support or exhausted_reconciliation
            quota = db.execute("SELECT MIN(r.next_at) FROM api_turn_retries r JOIN api_inbox i "
                "ON i.id=r.event_id WHERE i.session_id=? AND r.status='waiting_for_quota'",
                (session_id,)).fetchone()[0]
            cursor = db.execute("SELECT COALESCE(MAX(cursor),0) FROM api_events WHERE session_id=?",
                                (session_id,)).fetchone()[0]
            selected = events[:limit]
            return {"session_id": session_id, "state": json.loads(row["projection"]),
                    "events": [{"cursor": e["cursor"], "event_id": e["id"], "type": e["type"],
                                "occurred_at": e["created_at"], "payload": json.loads(e["payload"])}
                               for e in selected], "pending_count": 0 if needs_support else pending,
                    "needs_support": needs_support,
                    "waiting_for_quota": quota is not None, "retry_at": quota,
                    "blocked_count": (pending + int(exhausted_reconciliation)) if needs_support else 0,
                    "next_cursor": selected[-1]["cursor"] if selected else after,
                    "current_cursor": cursor, "has_more": len(events) > limit}
