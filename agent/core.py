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
from langchain.agents.structured_output import ToolStrategy
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
        tools: Sequence[AgentTool] = (),
        middleware: Sequence[AgentMiddleware[Any, Any]] = (),
        checkpointer: Any | None = None,
        store: Any | None = None,
    ):
        self.settings = settings
        self.tools = tuple(tools)
        self.middleware = tuple(middleware)
        self.checkpointer = checkpointer or InMemorySaver()
        self.store = store or InMemoryStore()
        self.policy = ModelEscalationPolicy(settings.confidence_threshold)
        api_key = settings.require_api_key()
        self.nano_model = ChatOpenAI(
            model=settings.nano_model,
            api_key=api_key,
            reasoning_effort="minimal",
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
        return create_agent(
            model=model,
            tools=selected_tools,
            system_prompt=SYSTEM_PROMPT,
            middleware=(*self.middleware, *limits),
            response_format=ToolStrategy(
                ScamAssessment,
                handle_errors=(
                    "스키마를 다시 확인하세요. 확인되지 않은 값은 null 또는 unverified로 "
                    "표시하고 immediate_actions는 priority 순으로 반환하세요."
                ),
            ),
            state_schema=UnHookState,
            context_schema=UnHookRuntimeContext,
            checkpointer=self.checkpointer,
            store=self.store,
            name="unhook_agent",
        )

    def _invoke_once(
        self,
        *,
        model: ChatOpenAI,
        model_name: str,
        turn: AgentTurnInput,
        thread_id: str,
    ) -> tuple[ScamAssessment, dict[str, Any]]:
        payload = build_turn_payload(
            user_statement=turn.user_statement,
            quoted_content=turn.quoted_content,
            state=turn.state,
            tool_results=turn.tool_results,
            age_group=turn.age_group,
        )
        graph = self._graph(model, turn)
        state_input = {
            "messages": [HumanMessage(content=payload)],
            **turn.state.model_dump(),
            "tool_results": turn.tool_results,
        }
        result = graph.invoke(
            state_input,
            config={
                "configurable": {"thread_id": thread_id},
                "recursion_limit": self.settings.recursion_limit,
                "tags": ["unhook", model_name],
            },
            context=UnHookRuntimeContext(
                user_id=turn.user_id,
                age_group=turn.age_group,  # type: ignore[arg-type]
            ),
        )
        assessment = ScamAssessment.model_validate(result["structured_response"])
        return assessment, result

    def invoke(self, turn: AgentTurnInput) -> AgentRunResult:
        """Run one user turn and optionally escalate once to the review model."""
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
        payload = build_turn_payload(
            user_statement=turn.user_statement,
            quoted_content=turn.quoted_content,
            state=turn.state,
            tool_results=turn.tool_results,
            age_group=turn.age_group,
        )
        result = await self._graph(chosen_model, turn).ainvoke(
            {
                "messages": [HumanMessage(content=payload)],
                **turn.state.model_dump(),
                "tool_results": turn.tool_results,
            },
            config={
                "configurable": {"thread_id": chosen_thread},
                "recursion_limit": self.settings.recursion_limit,
                "tags": ["unhook", chosen_name],
            },
            context=UnHookRuntimeContext(
                user_id=turn.user_id,
                age_group=turn.age_group,  # type: ignore[arg-type]
            ),
        )
        assessment = ScamAssessment.model_validate(result["structured_response"])
        after = self.policy.after_nano(
            assessment.confidence,
            turn.state,
            emergency_detected=turn.emergency_detected,
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
        review_payload = build_turn_payload(
            user_statement=review_turn.user_statement,
            quoted_content=review_turn.quoted_content,
            state=review_turn.state,
            tool_results=review_turn.tool_results,
            age_group=review_turn.age_group,
        )
        review_result = await self._graph(self.review_model, review_turn).ainvoke(
            {
                "messages": [HumanMessage(content=review_payload)],
                **review_turn.state.model_dump(),
                "tool_results": review_turn.tool_results,
            },
            config={
                "configurable": {
                    "thread_id": f"{turn.thread_id}:review:{turn.conversation_turns}"
                },
                "recursion_limit": self.settings.recursion_limit,
                "tags": ["unhook", self.settings.review_model],
            },
            context=UnHookRuntimeContext(
                user_id=turn.user_id,
                age_group=turn.age_group,  # type: ignore[arg-type]
            ),
        )
        return AgentRunResult(
            assessment=ScamAssessment.model_validate(review_result["structured_response"]),
            model_used=self.settings.review_model,
            escalated=True,
            escalation_reasons=after.reasons,
            raw_state=review_result,
        )


def build_unhook_agent(
    *,
    tools: Sequence[AgentTool] = (),
    middleware: Sequence[AgentMiddleware[Any, Any]] = (),
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
