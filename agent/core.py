"""LangChain Agent Core. Tool and custom middleware implementations are injected by teammates."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from langchain.agents import create_agent
from langchain.agents.middleware import (
    AgentMiddleware,
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
)
from langchain.agents.structured_output import ProviderStrategy
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import AgentSettings
from .model_policy import EscalationDecision, ModelEscalationPolicy
from .prompts import SYSTEM_PROMPT, build_turn_payload
from .schemas import ScamAssessment, StateSnapshot, UnHookRuntimeContext, UnHookState

AgentTool = BaseTool | Callable[..., Any] | dict[str, Any]
ALLOWED_TOOL_NAMES = frozenset(
    {
        "check_url_risk",
        "verify_caller_number",
        "get_scam_playbook",
        "report_to_authority",
    }
)
LOOKUP_TOOL_NAMES = frozenset({"check_url_risk", "verify_caller_number"})
REPORT_TOOL_NAME = "report_to_authority"


def _load_default_tools() -> tuple[AgentTool, ...]:
    """Load the repository's canonical Tool list without creating a cycle."""
    from tools import ALL_TOOLS

    return tuple(ALL_TOOLS)


def _strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Rewrite a Pydantic JSON schema to OpenAI strict-mode form.

    Strict mode requires every property to be listed in ``required`` and
    ``additionalProperties: false`` on every object. Optional fields keep their
    ``null`` alternative, so Pydantic validation is unchanged.
    """
    if isinstance(schema, dict):
        out: dict[str, Any] = {}
        for key, value in schema.items():
            if key == "default":
                continue
            out[key] = _strict_json_schema(value)
        if out.get("type") == "object" and isinstance(out.get("properties"), dict):
            out["required"] = list(out["properties"].keys())
            out["additionalProperties"] = False
        return out
    if isinstance(schema, list):
        return [_strict_json_schema(item) for item in schema]
    return schema


def _strict_response_format() -> ProviderStrategy[ScamAssessment]:
    """Provider-native structured output with strict schema enforcement.

    Without ``strict`` the model may omit required fields such as
    ``injection_detected`` and the turn fails at validation time.
    """
    strategy = ProviderStrategy(ScamAssessment, strict=True)
    strategy.schema_spec.json_schema = _strict_json_schema(strategy.schema_spec.json_schema)
    return strategy


def _assessment_from_result(result: dict[str, Any]) -> ScamAssessment:
    """Validate Agent output and explain an incomplete Agent loop clearly."""
    structured = result.get("structured_response")
    if structured is not None:
        return ScamAssessment.model_validate(structured)

    called_tools: list[str] = []
    for message in result.get("messages", []):
        for call in getattr(message, "tool_calls", None) or []:
            name = call.get("name")
            if name and name != "ScamAssessment":
                called_tools.append(str(name))
    detail = ", ".join(dict.fromkeys(called_tools)) or "없음"
    raise RuntimeError(
        "Agent가 최종 ScamAssessment를 생성하지 못했습니다. "
        f"실행 중 요청한 Tool: {detail}"
    )


def _output_audit_failed(result: dict[str, Any]) -> bool:
    """Read OutputAuditMiddleware's same-turn review signal."""
    for message in reversed(result.get("messages", [])):
        metadata = getattr(message, "response_metadata", None) or {}
        audit = metadata.get("unhook_audit")
        if isinstance(audit, dict):
            return bool(audit.get("failed"))
    return False


class AgentTurnInput(BaseModel):
    """Safe hand-off contract from the app/guardrail layer to Agent Core."""

    model_config = ConfigDict(extra="forbid")

    thread_id: str = Field(min_length=1, max_length=200)
    user_id: str = Field(min_length=1, max_length=200)
    age_group: str = Field(default="general", pattern=r"^(general|senior)$")
    user_statement: str = Field(default="", max_length=12000)
    quoted_content: str = Field(default="", max_length=12000)
    state: StateSnapshot = Field(default_factory=StateSnapshot)
    tool_results: dict[str, Any] = Field(default_factory=dict)
    multiple_messages: bool = False
    conversation_turns: int = Field(default=0, ge=0)
    tool_conflict: bool = False
    output_audit_failed: bool = False
    report_approved: bool = False
    emergency_detected: bool = False

    @field_validator("state", mode="before")
    @classmethod
    def adapt_shared_state(cls, value: Any) -> Any:
        """Accept the shared UnHookState dict while excluding private fields."""
        if isinstance(value, dict):
            return StateSnapshot.from_state(value)
        return value


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    assessment: ScamAssessment
    model_used: str
    escalated: bool
    escalation_reasons: tuple[str, ...] = ()
    raw_state: dict[str, Any] = field(default_factory=dict, repr=False)


def _tool_name(tool: AgentTool) -> str:
    if isinstance(tool, dict):
        function = tool.get("function")
        if isinstance(function, dict) and function.get("name"):
            return str(function["name"])
        return str(tool.get("name", ""))
    return str(getattr(tool, "name", getattr(tool, "__name__", "")))


def select_tools(tools: Sequence[AgentTool], turn: AgentTurnInput) -> list[AgentTool]:
    """Apply emergency and explicit-approval boundaries before model binding."""
    selected: list[AgentTool] = []
    for tool in tools:
        name = _tool_name(tool)
        if name not in ALLOWED_TOOL_NAMES:
            continue
        if (turn.emergency_detected or turn.state.money_sent is True) and name in LOOKUP_TOOL_NAMES:
            continue
        if name == REPORT_TOOL_NAME and not turn.report_approved:
            continue
        selected.append(tool)
    return selected


class UnHookAgent:
    def __init__(
        self,
        *,
        settings: AgentSettings,
        tools: Sequence[AgentTool] | None = None,
        middleware: Sequence[AgentMiddleware[Any, Any]] | None = None,
        checkpointer: Any | None = None,
        store: Any | None = None,
    ):
        self.settings = settings
        self.tools = tuple(tools) if tools is not None else _load_default_tools()
        self.checkpointer = checkpointer or InMemorySaver()
        self.store = store or InMemoryStore()
        self.policy = ModelEscalationPolicy(settings.confidence_threshold)
        api_key = settings.require_api_key()
        self.nano_model = ChatOpenAI(
            model=settings.nano_model,
            api_key=api_key,
            # "minimal" makes gpt-5 skip tool calls and answer directly;
            # "low" keeps latency small while still planning lookups.
            reasoning_effort="low",
            timeout=settings.nano_timeout_seconds,
            max_retries=0,
            max_completion_tokens=settings.nano_max_output_tokens,
        )
        self.review_model = ChatOpenAI(
            model=settings.review_model,
            api_key=api_key,
            reasoning_effort="medium",
            timeout=settings.review_timeout_seconds,
            max_retries=0,
            max_completion_tokens=settings.review_max_output_tokens,
        )
        if middleware is None:
            from middleware import build_middleware
            from schemas import InjectionDecision

            classifier = self.nano_model.with_structured_output(InjectionDecision)
            self.middleware = tuple(build_middleware(classifier=classifier))
        else:
            self.middleware = tuple(middleware)
        self.guarded_input = any(
            type(item).__name__ == "ContentIsolationMiddleware"
            for item in self.middleware
        )

    def _graph(self, model: ChatOpenAI, turn: AgentTurnInput):
        selected_tools = select_tools(self.tools, turn)
        limits: tuple[AgentMiddleware[Any, Any], ...] = (
            ModelCallLimitMiddleware(
                run_limit=self.settings.model_call_limit,
                exit_behavior="error",
            ),
            ToolCallLimitMiddleware(
                run_limit=self.settings.tool_call_limit,
                exit_behavior="error",
            ),
        )
        middleware = self.middleware
        # report_approved is the application's recorded HITL approval. Once it is
        # true, do not interrupt the already-approved call a second time.
        if turn.report_approved:
            middleware = tuple(
                item for item in middleware
                if type(item).__name__ != "HumanInTheLoopMiddleware"
            )
        return create_agent(
            model=model,
            tools=selected_tools,
            system_prompt=SYSTEM_PROMPT,
            middleware=(*middleware, *limits),
            # OpenAI's provider-native structured output in strict mode. Unlike
            # ToolStrategy, it does not force another tool call after the
            # requested lookup has completed, and strict mode guarantees every
            # schema field is present.
            response_format=_strict_response_format(),
            state_schema=UnHookState,
            context_schema=UnHookRuntimeContext,
            checkpointer=self.checkpointer,
            store=self.store,
            name="unhook_agent",
        )

    @staticmethod
    def _config(thread_id: str, model_name: str, recursion_limit: int) -> dict[str, Any]:
        return {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": recursion_limit,
            "tags": ["unhook", model_name],
        }

    def _state_input(self, graph: Any, turn: AgentTurnInput, config: dict[str, Any]) -> dict[str, Any]:
        payload = build_turn_payload(
            user_statement=turn.user_statement,
            quoted_content="",
            state=turn.state,
            tool_results=turn.tool_results,
            age_group=turn.age_group,
        )
        state_input: dict[str, Any] = {
            **turn.state.model_dump(),
            "tool_results": turn.tool_results,
        }
        if not self.guarded_input:
            state_input["messages"] = [HumanMessage(content=build_turn_payload(
                user_statement=turn.user_statement,
                quoted_content=turn.quoted_content,
                state=turn.state,
                tool_results=turn.tool_results,
                age_group=turn.age_group,
            ))]
            return state_input

        from pii import prepare_masked_input

        prior_vault: dict[str, str] = {}
        try:
            snapshot = graph.get_state(config)
            values = getattr(snapshot, "values", {}) or {}
            prior_vault = dict(values.get("pii_vault") or {})
        except (KeyError, LookupError, ValueError):
            # A new thread has no checkpoint yet.
            pass
        prepared = prepare_masked_input(
            payload,
            [turn.quoted_content] if turn.quoted_content else [],
            vault=prior_vault,
        )
        state_input["messages"] = [prepared.message]
        state_input["pii_vault"] = prepared.vault
        return state_input

    @staticmethod
    def _normalize_emergency(turn: AgentTurnInput) -> AgentTurnInput:
        if turn.emergency_detected or turn.state.money_sent is True:
            return turn
        from middleware import detect_money_sent

        if detect_money_sent(turn.user_statement):
            return turn.model_copy(update={"emergency_detected": True})
        return turn

    def _invoke_once(
        self,
        *,
        model: ChatOpenAI,
        model_name: str,
        turn: AgentTurnInput,
        thread_id: str,
    ) -> tuple[ScamAssessment, dict[str, Any]]:
        graph = self._graph(model, turn)
        config = self._config(thread_id, model_name, self.settings.recursion_limit)
        state_input = self._state_input(graph, turn, config)
        result = graph.invoke(
            state_input,
            config=config,
            context=UnHookRuntimeContext(
                user_id=turn.user_id,
                age_group=turn.age_group,  # type: ignore[arg-type]
            ),
        )
        assessment = _assessment_from_result(result)
        return assessment, result

    def invoke(self, turn: AgentTurnInput) -> AgentRunResult:
        """Run one user turn and optionally escalate once to the review model."""
        turn = self._normalize_emergency(turn)
        before = self.policy.before_call(
            user_statement=turn.user_statement,
            quoted_content=turn.quoted_content,
            multiple_messages=turn.multiple_messages,
            conversation_turns=turn.conversation_turns,
            tool_conflict=turn.tool_conflict,
            output_audit_failed=turn.output_audit_failed,
            state=turn.state,
            emergency_detected=turn.emergency_detected,
        )
        if before.use_review_model:
            assessment, raw = self._invoke_once(
                model=self.review_model,
                model_name=self.settings.review_model,
                turn=turn,
                thread_id=f"{turn.thread_id}:review:{turn.conversation_turns}",
            )
            return AgentRunResult(
                assessment=assessment,
                model_used=self.settings.review_model,
                escalated=True,
                escalation_reasons=before.reasons,
                raw_state=raw,
            )

        assessment, raw = self._invoke_once(
            model=self.nano_model,
            model_name=self.settings.nano_model,
            turn=turn,
            thread_id=turn.thread_id,
        )
        after = self.policy.after_nano(
            assessment.confidence,
            turn.state,
            emergency_detected=turn.emergency_detected,
            output_audit_failed=_output_audit_failed(raw),
        )
        if not after.use_review_model:
            return AgentRunResult(
                assessment=assessment,
                model_used=self.settings.nano_model,
                escalated=False,
                escalation_reasons=before.reasons,
                raw_state=raw,
            )

        review_turn = turn.model_copy(
            update={
                "tool_results": {
                    **turn.tool_results,
                    "nano_assessment_for_review": assessment.model_dump(mode="json"),
                }
            }
        )
        reviewed, review_raw = self._invoke_once(
            model=self.review_model,
            model_name=self.settings.review_model,
            turn=review_turn,
            thread_id=f"{turn.thread_id}:review:{turn.conversation_turns}",
        )
        return AgentRunResult(
            assessment=reviewed,
            model_used=self.settings.review_model,
            escalated=True,
            escalation_reasons=after.reasons,
            raw_state=review_raw,
        )

    async def ainvoke(self, turn: AgentTurnInput) -> AgentRunResult:
        """Async equivalent of invoke for FastAPI integration."""
        turn = self._normalize_emergency(turn)
        before = self.policy.before_call(
            user_statement=turn.user_statement,
            quoted_content=turn.quoted_content,
            multiple_messages=turn.multiple_messages,
            conversation_turns=turn.conversation_turns,
            tool_conflict=turn.tool_conflict,
            output_audit_failed=turn.output_audit_failed,
            state=turn.state,
            emergency_detected=turn.emergency_detected,
        )
        chosen_model = self.review_model if before.use_review_model else self.nano_model
        chosen_name = self.settings.review_model if before.use_review_model else self.settings.nano_model
        chosen_thread = (
            f"{turn.thread_id}:review:{turn.conversation_turns}"
            if before.use_review_model
            else turn.thread_id
        )
        graph = self._graph(chosen_model, turn)
        config = self._config(chosen_thread, chosen_name, self.settings.recursion_limit)
        result = await graph.ainvoke(
            self._state_input(graph, turn, config),
            config=config,
            context=UnHookRuntimeContext(
                user_id=turn.user_id,
                age_group=turn.age_group,  # type: ignore[arg-type]
            ),
        )
        assessment = _assessment_from_result(result)
        after = self.policy.after_nano(
            assessment.confidence,
            turn.state,
            emergency_detected=turn.emergency_detected,
            output_audit_failed=_output_audit_failed(result),
        )
        if before.use_review_model or not after.use_review_model:
            return AgentRunResult(
                assessment=assessment,
                model_used=chosen_name,
                escalated=before.use_review_model,
                escalation_reasons=before.reasons,
                raw_state=result,
            )
        # Keep the async review path explicit rather than calling sync code in the event loop.
        review_turn = turn.model_copy(
            update={
                "tool_results": {
                    **turn.tool_results,
                    "nano_assessment_for_review": assessment.model_dump(mode="json"),
                }
            }
        )
        review_thread = f"{turn.thread_id}:review:{turn.conversation_turns}"
        review_graph = self._graph(self.review_model, review_turn)
        review_config = self._config(
            review_thread, self.settings.review_model, self.settings.recursion_limit
        )
        review_result = await review_graph.ainvoke(
            self._state_input(review_graph, review_turn, review_config),
            config=review_config,
            context=UnHookRuntimeContext(
                user_id=turn.user_id,
                age_group=turn.age_group,  # type: ignore[arg-type]
            ),
        )
        return AgentRunResult(
            assessment=_assessment_from_result(review_result),
            model_used=self.settings.review_model,
            escalated=True,
            escalation_reasons=after.reasons,
            raw_state=review_result,
        )


def build_unhook_agent(
    *,
    tools: Sequence[AgentTool] | None = None,
    middleware: Sequence[AgentMiddleware[Any, Any]] | None = None,
    settings: AgentSettings | None = None,
    checkpointer: Any | None = None,
    store: Any | None = None,
) -> UnHookAgent:
    """Public factory used by the integration layer."""
    return UnHookAgent(
        settings=settings or AgentSettings.from_env(),
        tools=tools,
        middleware=middleware,
        checkpointer=checkpointer,
        store=store,
    )
