"""One graph writer per session; the disk inbox survives process restarts."""
import asyncio
import logging
from datetime import datetime

from app.adapters.extractor import ExtractorError

logger = logging.getLogger(__name__)


class Coordinator:
    def __init__(self, store, graph, *, concurrency=4):
        if type(concurrency) is not int or concurrency < 1:
            raise ValueError("Worker concurrency must be a positive integer")
        self.store = store
        self.graph = graph
        self.concurrency = asyncio.Semaphore(concurrency)
        self.max_tasks = concurrency
        self.tasks = {}
        self.wakeup = asyncio.Event()
        self.running = True
        self._prefer_reconciliation = True

    async def run(self):
        while self.running:
            queues = [(self.store.pending_sessions, self.run_session),
                      (self.store.due_reconciliations, self.run_reconciliation)]
            if self._prefer_reconciliation:
                queues.reverse()
            self._prefer_reconciliation = not self._prefer_reconciliation
            for pending, process in queues:
                if len(self.tasks) >= self.max_tasks:
                    break
                for session_id in pending():
                    if len(self.tasks) >= self.max_tasks:
                        break
                    if session_id not in self.tasks:
                        task = asyncio.create_task(process(session_id))
                        self.tasks[session_id] = task
                        task.add_done_callback(lambda _, sid=session_id: self._task_done(sid))
            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=0.5)
            except TimeoutError:
                pass
            self.wakeup.clear()

    def _task_done(self, session_id):
        self.tasks.pop(session_id, None)
        self.wakeup.set()

    async def run_session(self, session_id):
        async with self.concurrency:
            while self.running:
                event = self.store.next_pending(session_id)
                if not event:
                    return
                payload = event["payload"]
                delivered = list(set(self.store.delivered(session_id) +
                                     payload.get("rendered_response_ids", [])))
                try:
                    state = await self.graph.process(
                        session_id=session_id, event_id=event["id"],
                        text=payload.get("text", ""), action=payload.get("action"),
                        delivered_response_ids=delivered,
                        reply_to_response_id=payload.get("reply_to_response_id"),
                        ingress_guard=lambda: self.store.current_ingress(session_id, event["seq"]),
                        occurred_at=datetime.fromisoformat(event["created_at"]).timestamp(),
                    )
                    self.store.complete(event, state)
                    # Yield the slot after one turn so a session cannot retain
                    # a permit indefinitely while more messages arrive.
                    return
                except asyncio.CancelledError:
                    raise
                except ExtractorError as exc:
                    if exc.code in {"RATE_LIMITED", "RATE_LIMIT", "QUOTA_EXCEEDED"}:
                        self.store.wait_for_quota(event, getattr(exc, "retry_after_seconds", 60),
                            request_sent=getattr(exc, "request_sent", None))
                    else:
                        self.store.defer(event, exc.code, retryable=exc.retryable)
                    return
                except Exception as exc:
                    # Do not put customer text, credentials or HTTP bodies in logs.
                    logger.error("Turn failed event=%s type=%s", event["id"], type(exc).__name__)
                    self.store.defer(event, "TURN_PROCESSING_ERROR")
                    return

    async def run_reconciliation(self, session_id):
        async with self.concurrency:
            event_id = self.store.claim_reconciliation(session_id)
            if not event_id:
                return
            try:
                state = await self.graph.reconcile(session_id, event_id)
                self.store.publish_reconciliation(session_id, event_id, state)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Reconciliation failed session=%s type=%s", session_id, type(exc).__name__)
                self.store.reconciliation_failed(session_id, event_id)

    async def close(self):
        self.running = False
        self.wakeup.set()
        for task in list(self.tasks.values()):
            task.cancel()
        await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
