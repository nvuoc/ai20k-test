"""Extract one complete chat turn without applying it to booking state.

Provider output is untrusted. Validate JSON, the typed contract, catalog codes
and candidate references before returning. Semantic review/confirmation scope,
maps, reducers and booking operations belong to subsequent domain components.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from importlib.resources import files
from typing import Any, Protocol

from pydantic import ValidationError

from app.contracts.nlu import (
    NluInput,
    NluResult,
    provider_output_schema,
    validate_candidate_references,
)
from app.contracts.registry import (
    CONTRACT_VERSION,
    DEFAULT_PAYMENT_CODES,
    DEFAULT_SPECIAL_REQUEST_CODES,
    DEFAULT_VEHICLE_CODES,
    INTENT_NAMES,
    MAX_ACTS,
    SPEECH_STATUSES,
    SpeechStatus,
)
from app.contracts.turn import TurnInput, TurnResult, turn_output_schema

DEFAULT_MODEL = "gpt-4.1-mini-2025-04-14"
PROMPT_VERSION = "extractor-v1"
MAX_OUTPUT_CHARS = 64_000


class ExtractorError(Exception):
    """Safe, typed adapter failure. Never contains customer text/provider bodies."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ModelReply:
    text: str | None = None
    refusal: str | None = None
    incomplete: bool = False


class ExtractorClient(Protocol):
    async def generate(
        self,
        *,
        system_prompt: str,
        input_json: str,
        response_schema: dict[str, Any],
        model: str,
        timeout_seconds: float,
    ) -> ModelReply: ...


@dataclass(slots=True)
class CallBudget:
    """Shared per-turn inference budget, including any subsequent semantic review."""

    limit: int = 2
    used: int = 0

    def __post_init__(self) -> None:
        if type(self.limit) is not int or not 1 <= self.limit <= 2:
            raise ValueError("call budget limit must be 1 or 2")
        if type(self.used) is not int or not 0 <= self.used <= self.limit:
            raise ValueError("call budget used must be between zero and limit")

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def consume(self) -> None:
        if self.remaining == 0:
            raise ExtractorError("CALL_BUDGET_EXHAUSTED", "No model calls remain for this turn.")
        self.used += 1


def _default_aliases() -> dict[str, dict[str, str]]:
    return {
        "vehicle_type": {
            "xe 4 chỗ": "oto_4_cho",
            "ô tô 4 chỗ": "oto_4_cho",
            "xe 7 chỗ": "oto_7_cho",
            "ô tô 7 chỗ": "oto_7_cho",
            "xe máy": "xe_may",
            "xe máy điện": "xe_may_dien",
        },
        "payment_method": {
            "tiền mặt": "cash",
            "thẻ đã liên kết": "linked_card",
            "công ty trả": "corporate",
        },
        "special_requests": {
            "ghế trẻ em": "child_seat",
            "hỗ trợ xe lăn": "wheelchair_access",
            "thú cưng": "pet",
        },
    }


@dataclass(frozen=True, slots=True)
class ExtractorRuntime:
    client: ExtractorClient
    model: str = DEFAULT_MODEL
    timeout_seconds: float = 12.0
    deadline: float | None = None
    call_budget: CallBudget = field(default_factory=CallBudget)
    trusted_speech_status: SpeechStatus | None = None
    vehicle_codes: frozenset[str] = DEFAULT_VEHICLE_CODES
    payment_codes: frozenset[str] = DEFAULT_PAYMENT_CODES
    special_request_codes: frozenset[str] = DEFAULT_SPECIAL_REQUEST_CODES
    catalog_aliases: Mapping[str, Mapping[str, str]] = field(default_factory=_default_aliases)
    max_acts: int = MAX_ACTS
    contract_version: str = CONTRACT_VERSION
    prompt_version: str = PROMPT_VERSION
    max_attempts: int = 2

    def __post_init__(self) -> None:
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a positive finite number")
        if self.deadline is not None and (
            isinstance(self.deadline, bool)
            or not isinstance(self.deadline, (int, float))
            or not math.isfinite(self.deadline)
        ):
            raise ValueError("deadline must be an absolute monotonic timestamp")
        if (
            self.trusted_speech_status is not None
            and self.trusted_speech_status not in SPEECH_STATUSES
        ):
            raise ValueError("unknown trusted speech status")
        if type(self.max_acts) is not int or not 1 <= self.max_acts <= MAX_ACTS:
            raise ValueError("max_acts must be an integer from 1 to 24")
        if type(self.max_attempts) is not int or self.max_attempts not in {1, 2}:
            raise ValueError("max_attempts must be 1 or 2")
        if self.contract_version != CONTRACT_VERSION or self.prompt_version != PROMPT_VERSION:
            raise ValueError("unsupported contract or prompt version")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("model must be a nonblank identifier")
        if not isinstance(self.call_budget, CallBudget):
            raise ValueError("call_budget must be a shared CallBudget")
        for name in ("vehicle_codes", "payment_codes", "special_request_codes"):
            codes = getattr(self, name)
            if isinstance(codes, str) or not isinstance(codes, (set, frozenset, tuple, list)):
                raise ValueError("catalog codes must be a collection of strings")
            if any(not isinstance(code, str) or not code.strip() for code in codes):
                raise ValueError("catalog codes must be nonblank strings")
            object.__setattr__(self, name, frozenset(codes))
        # Copy application configuration. Aliases whose codes are disabled are omitted.
        catalogs = _catalogs(self)
        aliases: dict[str, dict[str, str]] = {}
        for target, entries in self.catalog_aliases.items():
            if target not in catalogs or not isinstance(entries, Mapping):
                raise ValueError("invalid catalog alias target")
            aliases[target] = {}
            for label, code in entries.items():
                if not isinstance(label, str) or not label.strip() or not isinstance(code, str):
                    raise ValueError("catalog aliases must map nonblank labels to strings")
                if code in catalogs[target]:
                    aliases[target][label] = code
        object.__setattr__(self, "catalog_aliases", aliases)


def _catalogs(runtime: ExtractorRuntime) -> dict[str, frozenset[str]]:
    return {
        "vehicle_type": runtime.vehicle_codes,
        "payment_method": runtime.payment_codes,
        "special_requests": runtime.special_request_codes,
    }


def _validate_catalog_value(target: str, value: Any, runtime: ExtractorRuntime) -> None:
    catalog = _catalogs(runtime).get(target)
    if catalog is None or value is None:
        return
    codes = value if target == "special_requests" else [value]
    if any(code not in catalog for code in codes):
        raise ValueError("catalog_code_not_registered")


def _build_prompt(runtime: ExtractorRuntime, *, v2: bool = False, turn_version: str = "parrotgo-turn-2") -> str:
    base = files("app.prompts").joinpath("extractor_v1.txt").read_text(encoding="utf-8")
    registry = {
        "contract_version": runtime.contract_version,
        "prompt_version": runtime.prompt_version,
        "intent_registry": list(INTENT_NAMES),
        "catalog_codes": {target: sorted(codes) for target, codes in _catalogs(runtime).items()},
        "catalog_aliases": runtime.catalog_aliases,
        "max_acts": runtime.max_acts,
    }
    if v2:
        # Share slot/intent rules, but never send incompatible v1 JSON examples.
        base = base[base.index("SPEECH_STATUS"):base.index("VÍ DỤ MINH HỌA")]
        base += "\n" + files("app.prompts").joinpath("turn_v2.txt").read_text(encoding="utf-8").replace("parrotgo-turn-2", turn_version)
        registry.update(contract_version=turn_version, prompt_version="extractor-v3" if turn_version == "parrotgo-turn-3" else "extractor-v2")
    return base + "\nAPP_REGISTRY (dữ liệu cấu hình):\n" + json.dumps(registry, ensure_ascii=False)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(_: str) -> Any:
    raise ValueError("nonstandard_json_number")


def _parse_reply(reply: ModelReply, data: NluInput, runtime: ExtractorRuntime) -> NluResult | TurnResult:
    if not isinstance(reply, ModelReply):
        raise ExtractorError("PROVIDER_ERROR", "Provider returned an unsupported reply object.")
    if reply.refusal is not None:
        raise ExtractorError("MODEL_REFUSAL", "The model refused extraction.")
    if reply.incomplete:
        raise ExtractorError("MODEL_INCOMPLETE", "The model response was incomplete.")
    if not isinstance(reply.text, str) or not reply.text or len(reply.text) > MAX_OUTPUT_CHARS:
        raise ExtractorError("OUTPUT_INVALID", "Model output is missing or exceeds its limit.")
    try:
        payload = json.loads(
            reply.text, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
        result = TurnResult.model_validate(payload) if isinstance(data, TurnInput) else NluResult.model_validate(payload)
        acts = result.booking_acts if isinstance(result, TurnResult) else result.dialogue_acts
        if isinstance(result, TurnResult):
            if result.contract_version != data.contract_version:
                raise ValueError("turn_contract_version_mismatch")
            result.align_literal_evidence(data)
            result.validate_evidence(data)
            for question in result.questions:
                _validate_catalog_value("vehicle_type", question.vehicle_ref, runtime)
        if len(acts) > runtime.max_acts:
            raise ValueError("act_limit_exceeded")
        for act in acts:
            if act.intent in {"provide_info", "change_info"}:
                _validate_catalog_value(act.target, act.value, runtime)
        # Inquiry references are checked against their scoped set by the domain.
        legacy = NluResult.model_validate({"speech_status": result.speech_status,
            "dialogue_acts": [a.model_dump(exclude={"evidence_span"}) for a in acts] or
            ([{"intent":"chit_chat", "target":None, "value":None}] if result.speech_status == "clear" else [])})
        validate_candidate_references(legacy, data)
        if result.speech_status in {"no_speech", "noise"}:
            raise ValueError("speech_absence_requires_trusted_event")
    except (ValueError, TypeError, RecursionError):
        raise ExtractorError("OUTPUT_INVALID", "Model output violates the NLU contract.") from None
    if runtime.trusted_speech_status == "low_confidence" and result.speech_status == "clear":
        # A text-only model cannot upgrade an authoritative low-confidence audio event.
        result = result.model_copy(update={"speech_status": "low_confidence"})
    return result


async def llm_extractor_func(
    nlu_input: NluInput | Mapping[str, Any], *, runtime: ExtractorRuntime
) -> NluResult | TurnResult:
    """Return validated interpretation; raise ExtractorError on input/provider errors.

    The caller owns state projection, complete-turn assembly and per-turn budget.
    Keep the same budget/deadline for any subsequent semantic review. Cancellation
    propagates; neither failed extraction nor successful extraction mutates input.
    """
    try:
        if not isinstance(nlu_input, (NluInput, Mapping)):
            raise ValueError("input must be an object projection")
        raw = nlu_input.model_dump() if isinstance(nlu_input, NluInput) else dict(nlu_input)
        data = (TurnInput if isinstance(nlu_input, TurnInput) or raw.get("contract_version") in {"parrotgo-turn-2", "parrotgo-turn-3"} else NluInput).model_validate(deepcopy(raw))
        for target in _catalogs(runtime):
            _validate_catalog_value(target, getattr(data.booking_state, target).value, runtime)
    except (ValidationError, ValueError, TypeError, RecursionError):
        raise ExtractorError(
            "INPUT_INVALID", "Input violates the NLU projection contract."
        ) from None

    if runtime.trusted_speech_status in {"no_speech", "noise"}:
        return NluResult(speech_status=runtime.trusted_speech_status, dialogue_acts=[])
    if data.utterance.text is None or not data.utterance.text.strip():
        raise ExtractorError(
            "EMPTY_TRANSCRIPT_UNCLASSIFIED", "Empty transcript requires a trusted speech event."
        )

    deadline = runtime.deadline
    if deadline is None:
        deadline = time.monotonic() + runtime.timeout_seconds
    v2 = isinstance(data, TurnInput)
    turn_version = data.contract_version if v2 else "parrotgo-turn-2"
    prompt = _build_prompt(runtime, v2=v2, turn_version=turn_version)
    original_json = json.dumps(data.model_dump(mode="json"), ensure_ascii=False, allow_nan=False)
    schema = turn_output_schema() if v2 else provider_output_schema()
    schema["properties"]["booking_acts" if v2 else "dialogue_acts"]["maxItems"] = runtime.max_acts
    attempt = 0
    while True:
        remaining_time = deadline - time.monotonic()
        if remaining_time <= 0:
            raise ExtractorError("DEADLINE_EXCEEDED", "Extraction deadline has expired.")
        runtime.call_budget.consume()
        attempt += 1
        request_timeout = min(runtime.timeout_seconds, remaining_time)
        timeout_scope = asyncio.timeout(request_timeout)
        try:
            async with timeout_scope:
                reply = await runtime.client.generate(
                    system_prompt=prompt,
                    input_json=original_json,
                    response_schema=deepcopy(schema),
                    model=runtime.model,
                    timeout_seconds=request_timeout,
                )
            # Cooperative asyncio timeouts cannot interrupt a client that blocks
            # the loop. Check again before and after validation to reject late data.
            _check_result_deadline(deadline, runtime)
            result = _parse_reply(reply, data, runtime)
            _check_result_deadline(deadline, runtime)
            return result
        except TimeoutError:
            # Windows timers can fire slightly before monotonic() reaches the
            # stored deadline. An expired scope that used the remaining turn
            # time has still consumed that allocation; do not retry it.
            turn_time_expired = deadline <= time.monotonic() or (
                timeout_scope.expired() and remaining_time <= runtime.timeout_seconds
            )
            if runtime.deadline is not None and turn_time_expired:
                error = ExtractorError("DEADLINE_EXCEEDED", "Extraction deadline has expired.")
            else:
                error = ExtractorError(
                    "PROVIDER_TIMEOUT", "Model request timed out.", retryable=not turn_time_expired
                )
        except ExtractorError as exc:
            error = exc
        except Exception:
            # Client implementation/SDK parsing errors must not leak provider bodies.
            # asyncio.CancelledError is a BaseException and deliberately propagates.
            error = ExtractorError("PROVIDER_ERROR", "Model client failed unexpectedly.")
        if attempt >= runtime.max_attempts or runtime.call_budget.remaining == 0:
            raise error from None
        if deadline <= time.monotonic():
            if runtime.deadline is not None:
                raise ExtractorError(
                    "DEADLINE_EXCEEDED", "Extraction deadline has expired."
                ) from None
            raise error from None
        if error.code == "OUTPUT_INVALID":
            # One fresh extraction with the SAME original input. Do not echo untrusted
            # invalid output, secrets or customer data into a system instruction.
            prompt = _build_prompt(runtime, v2=v2, turn_version=turn_version) + (
                "\nREPAIR: Output trước không hợp lệ (OUTPUT_INVALID). Đọc lại input gốc; "
                "kiểm tra kiểu intent/target/value, mã catalog và ID candidate. "
                "Không bổ sung dữ kiện. Chỉ trả JSON đúng contract."
            )
        elif not error.retryable:
            raise error from None


def _check_result_deadline(deadline: float, runtime: ExtractorRuntime) -> None:
    if time.monotonic() >= deadline:
        if runtime.deadline is not None:
            raise ExtractorError("DEADLINE_EXCEEDED", "Extraction deadline has expired.")
        raise ExtractorError("PROVIDER_TIMEOUT", "Extraction exceeded its time limit.")
