"""Thin graph orchestration; clients and callables never enter checkpoints."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, TypedDict

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph


class RuntimeState(TypedDict, total=False):
    state: dict
    incoming_event: dict | None
    interpretation: dict | None


class DurableGraph:
    """One session writer is supplied by the application's coordinator.

    Read/interpret work may run again if the process dies during a node. The
    sandbox ledger's immutable operation keys preserve transaction effects.
    Completed turns are deduplicated without invoking NLU or providers.
    """

    def __init__(self, engine: Any, checkpoint_path: str | Path) -> None:
        self.engine = engine
        self.checkpoint_path = str(checkpoint_path)
        self.graph = None
        self._saver_context = None
        self._guards: dict[str, Any] = {}

    async def __aenter__(self) -> DurableGraph:
        Path(self.checkpoint_path).parent.mkdir(parents=True, exist_ok=True)
        self._saver_context = AsyncSqliteSaver.from_conn_string(self.checkpoint_path)
        saver = await self._saver_context.__aenter__()
        await saver.setup()
        builder = StateGraph(RuntimeState)
        builder.add_node("process_turn", self._process_turn)
        if hasattr(self.engine, "prepare") and hasattr(self.engine, "interpret"):
            builder.add_node("interpret_turn", self._interpret_turn)
            builder.add_node("prepare_turn", self._prepare_turn)
            builder.add_edge(START, "interpret_turn")
            builder.add_edge("interpret_turn", "prepare_turn")
            builder.add_edge("prepare_turn", "process_turn")
        else:
            builder.add_edge(START, "process_turn")
        builder.add_edge("process_turn", END)
        self.graph = builder.compile(checkpointer=saver)
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        if self._saver_context is not None:
            await self._saver_context.__aexit__(exc_type, exc, traceback)
        self.graph = None

    @staticmethod
    def _config(session_id: str) -> dict:
        return {"configurable": {"thread_id": session_id}, "recursion_limit": 8}

    async def _process_turn(self, values: RuntimeState) -> RuntimeState:
        event = values.get("incoming_event")
        state = values["state"]
        if not event:
            return {"state": state, "incoming_event": None}
        session_id = state["control"]["session_id"]
        if event.get("type") == "reconcile":
            updated = await self.engine.reconcile(state, event["event_id"])
        elif hasattr(self.engine, "finalize"):
            updated = await self.engine.finalize(state, event, self._guards.get(session_id))
        else:
            updated = await self.engine.process(
                state, event["text"], action=event.get("action"),
                delivered_response_ids=event.get("delivered_response_ids", []),
                reply_to_response_id=event.get("reply_to_response_id"),
                event_id=event["event_id"], ingress_guard=self._guards.get(session_id),
            )
        return {"state": updated, "incoming_event": None, "interpretation": None}

    async def _interpret_turn(self, values: RuntimeState) -> RuntimeState:
        event = values.get("incoming_event")
        if not event or event.get("type") == "reconcile":
            return {"interpretation": None}
        result = await self.engine.interpret(values["state"], event)
        return {"interpretation": result}

    async def _prepare_turn(self, values: RuntimeState) -> RuntimeState:
        event = values.get("incoming_event")
        if not event or event.get("type") == "reconcile":
            return {}
        state = await self.engine.prepare(values["state"], event, values["interpretation"])
        return {"state": state}

    async def get_state(self, session_id: str) -> dict | None:
        snapshot = await self.graph.aget_state(self._config(session_id))
        return snapshot.values.get("state")

    async def initialize(self, session_id: str, state: dict) -> dict:
        existing = await self.get_state(session_id)
        if existing is not None:
            return existing
        await self.graph.aupdate_state(
            self._config(session_id), {"state": state, "incoming_event": None},
            as_node="process_turn",
        )
        return state

    async def process(self, session_id: str, event_id: str, text: str,
                      action: dict | None = None, delivered_response_ids: list[str] | None = None,
                      reply_to_response_id: str | None = None, ingress_guard: Any = None,
                      occurred_at: float | None = None) -> dict:
        config = self._config(session_id)
        snapshot = await self.graph.aget_state(config)
        state = snapshot.values.get("state")
        if state is None:
            raise ValueError("SESSION_CHECKPOINT_NOT_FOUND")
        if state["control"].get("last_event_id") == event_id and not snapshot.next:
            return state
        self._guards[session_id] = ingress_guard
        try:
            if snapshot.next:
                pending = snapshot.values.get("incoming_event")
                if pending and pending["event_id"] != event_id:
                    raise ValueError("PENDING_EVENT_REQUIRES_RESUME")
                output = await self.graph.ainvoke(None, config=config, durability="sync")
            else:
                event = {"event_id": event_id, "text": text, "action": action,
                         "delivered_response_ids": delivered_response_ids or [],
                         "reply_to_response_id": reply_to_response_id,
                         "occurred_at": occurred_at if occurred_at is not None else time.time()}
                output = await self.graph.ainvoke(
                    {"incoming_event": event}, config=config, durability="sync")
            return output["state"]
        finally:
            self._guards.pop(session_id, None)

    async def acknowledge(self, session_id: str, response_ids: list[str]) -> dict:
        config = self._config(session_id)
        snapshot = await self.graph.aget_state(config)
        if snapshot.next:
            raise ValueError("CANNOT_ACK_PENDING_TURN")
        state = snapshot.values.get("state")
        if state is None:
            raise ValueError("SESSION_CHECKPOINT_NOT_FOUND")
        state = self.engine.acknowledge(state, response_ids)
        await self.graph.aupdate_state(config, {"state": state}, as_node="process_turn")
        return state

    async def reconcile(self, session_id: str, event_id: str) -> dict:
        config = self._config(session_id)
        snapshot = await self.graph.aget_state(config)
        state = snapshot.values.get("state")
        if state is None:
            raise ValueError("SESSION_CHECKPOINT_NOT_FOUND")
        if state["control"].get("last_event_id") == event_id and not snapshot.next:
            return state
        if snapshot.next:
            pending = snapshot.values.get("incoming_event")
            if pending and pending["event_id"] != event_id:
                raise ValueError("PENDING_EVENT_REQUIRES_RESUME")
            output = await self.graph.ainvoke(None, config=config, durability="sync")
        else:
            output = await self.graph.ainvoke(
                {"incoming_event": {"event_id": event_id, "type": "reconcile"}},
                config=config, durability="sync")
        return output["state"]
