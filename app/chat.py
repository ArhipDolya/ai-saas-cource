import json
import logging
from dataclasses import dataclass
from typing import Annotated, TypedDict
from uuid import uuid4

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    RemoveMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from sqlalchemy.exc import SQLAlchemyError

from app.ai import (
    FREE_GEMINI_MODELS,
    GeminiAnalysisError,
    GeminiCompletionResult,
    GeminiConfigurationError,
    GeminiModelUnavailableError,
    request_gemini_contents,
    run_gemini_with_fallback,
)
from app.expenses import ExpenseService, TransactionValidationError
from app.prompts import build_project_chat_system_prompt
from app.tools import (
    build_readonly_finance_tools,
    langchain_tools_to_gemini_declarations,
)


CHAT_THREAD_ID_MAX_LENGTH = 128  # Bounded client-generated thread identifier length.
CHAT_MESSAGE_MAX_LENGTH = 2000  # Match frontend chat input limit to bound token cost.
MAX_CHAT_HISTORY_MESSAGES = 20  # Bound recent short-term memory turns sent to Gemini.
MAX_TOOL_ROUNDS = 5  # Prevent runaway agent -> tool loops per user message.
AGENT_NODE = "agent"
TOOLS_NODE = "tools"
GEMINI_USER_ROLE = "user"
GEMINI_MODEL_ROLE = "model"
GEMINI_RAW_PARTS_KEY = "gemini_raw_parts"
ERROR_KIND_CONFIG = "config"
ERROR_KIND_ANALYSIS = "analysis"
ERROR_KIND_DATABASE = "database"
TOOL_CALL_ID_PREFIX = "call_"
MIN_ASCII_PRINTABLE_ORD = 32
LOGGER = logging.getLogger(__name__)


class ChatValidationError(ValueError):
    """The AI chat request payload failed domain validation."""


@dataclass(frozen=True, kw_only=True)
class ChatTurnInput:
    thread_id: str
    message: str


@dataclass(frozen=True, kw_only=True)
class ProjectChatReply:
    thread_id: str
    answer: str
    model: str


class ChatGraphState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    system_prompt: str
    turn_human_message_id: str
    active_model: str
    reply_model: str
    tool_rounds: int
    error_kind: str
    error_detail: str


def parse_chat_payload(raw_payload: object) -> ChatTurnInput:
    """Validate incoming chat JSON payload and normalize `thread_id` and `message`."""
    if not isinstance(raw_payload, dict):
        raise ChatValidationError("Тіло запиту має бути JSON-об'єктом.")

    thread_id = _parse_thread_id(raw_payload.get("thread_id"))
    message = _parse_chat_message(raw_payload.get("message"))
    return ChatTurnInput(
        thread_id=thread_id,
        message=message,
    )


def _parse_thread_id(raw_thread_id: object) -> str:
    if not isinstance(raw_thread_id, str):
        raise ChatValidationError("Поле thread_id є обов'язковим рядком.")

    thread_id = raw_thread_id.strip()
    if not thread_id:
        raise ChatValidationError("Поле thread_id не може бути порожнім.")
    if len(thread_id) > CHAT_THREAD_ID_MAX_LENGTH:
        raise ChatValidationError(
            f"Поле thread_id має містити не більше {CHAT_THREAD_ID_MAX_LENGTH} символів.",
        )

    has_forbidden_chars = any(
        character.isspace() or ord(character) < MIN_ASCII_PRINTABLE_ORD
        for character in thread_id
    )
    if has_forbidden_chars:
        raise ChatValidationError(
            "Поле thread_id не може містити пробіли чи керуючі символи.",
        )
    return thread_id


def _parse_chat_message(raw_message: object) -> str:
    if not isinstance(raw_message, str):
        raise ChatValidationError("Поле message є обов'язковим рядком.")

    message = raw_message.strip()
    if not message:
        raise ChatValidationError("Повідомлення не може бути порожнім.")
    if len(message) > CHAT_MESSAGE_MAX_LENGTH:
        raise ChatValidationError(
            f"Повідомлення має містити не більше {CHAT_MESSAGE_MAX_LENGTH} символів.",
        )
    return message


class ProjectChatService:
    """Manage short-term memory and read-only tool calling in LangGraph."""

    def __init__(
        self,
        *,
        expense_service: ExpenseService,
        checkpointer: BaseCheckpointSaver | None = None,
        candidate_models: tuple[str, ...] = FREE_GEMINI_MODELS,
    ) -> None:
        self._expense_service = expense_service
        self._checkpointer: BaseCheckpointSaver = checkpointer or InMemorySaver()
        self._candidate_models = candidate_models
        self._tools: list[BaseTool] = build_readonly_finance_tools(
            expense_service=expense_service,
        )
        self._tool_declarations = langchain_tools_to_gemini_declarations(self._tools)
        self._tool_node = ToolNode(
            self._tools,
            handle_tool_errors=(TransactionValidationError, ValueError),
        )

        workflow: StateGraph[ChatGraphState] = StateGraph(ChatGraphState)
        workflow.add_node(AGENT_NODE, self._agent_node)
        workflow.add_node(TOOLS_NODE, self._run_tools_node)
        workflow.add_edge(START, AGENT_NODE)
        workflow.add_conditional_edges(
            AGENT_NODE,
            _route_after_agent,
            {
                TOOLS_NODE: TOOLS_NODE,
                END: END,
            },
        )
        workflow.add_conditional_edges(
            TOOLS_NODE,
            _route_after_tools,
            {
                AGENT_NODE: AGENT_NODE,
                END: END,
            },
        )
        self._graph = workflow.compile(checkpointer=self._checkpointer)

    @property
    def tool_names(self) -> tuple[str, ...]:
        """Expose registered tool names without allowing external mutation."""
        return tuple(current_tool.name for current_tool in self._tools)

    def answer_message(
        self,
        *,
        chat_input: ChatTurnInput,
    ) -> ProjectChatReply:
        """Invoke LangGraph workflow with `thread_id` persistence and read-only tools."""
        system_prompt = build_project_chat_system_prompt()
        turn_human_message_id = str(uuid4())
        config = _build_thread_config(thread_id=chat_input.thread_id)
        result_state = self._graph.invoke(
            {
                "messages": [
                    HumanMessage(
                        id=turn_human_message_id,
                        content=chat_input.message,
                    ),
                ],
                "system_prompt": system_prompt,
                "turn_human_message_id": turn_human_message_id,
                "active_model": "",
                "reply_model": "",
                "tool_rounds": 0,
                "error_kind": "",
                "error_detail": "",
            },
            config=config,
        )

        _raise_if_turn_failed(result_state)

        messages = result_state.get("messages", [])
        answer = _extract_latest_assistant_answer(messages)
        reply_model = str(result_state.get("reply_model", ""))
        LOGGER.info(
            "LangGraph AI chat reply generated",
            extra={
                "model": reply_model,
                "thread_id": chat_input.thread_id,
                "message_count": len(messages),
                "tool_rounds": result_state.get("tool_rounds", 0),
            },
        )
        return ProjectChatReply(
            thread_id=chat_input.thread_id,
            answer=answer,
            model=reply_model,
        )

    def get_thread_messages(
        self,
        *,
        thread_id: str,
    ) -> list[AnyMessage]:
        """Read persisted short-term memory messages for `thread_id` from checkpointer."""
        snapshot = self._graph.get_state(
            config=_build_thread_config(thread_id=thread_id),
        )
        raw_messages = snapshot.values.get("messages", [])
        return list(raw_messages) if isinstance(raw_messages, list) else []

    def _agent_node(
        self,
        state: ChatGraphState,
    ) -> dict[str, object]:
        messages = state["messages"]
        windowed_messages = _slice_clean_history_window(
            messages=messages,
            max_messages=MAX_CHAT_HISTORY_MESSAGES,
        )
        contents = _messages_to_gemini_contents(messages=windowed_messages)
        system_prompt = state["system_prompt"]
        tool_rounds = int(state.get("tool_rounds", 0))
        can_use_tools = tool_rounds < MAX_TOOL_ROUNDS
        tools_payload = self._tool_declarations if can_use_tools else None

        active_model = str(state.get("active_model", ""))
        models_for_step = (active_model,) if active_model else self._candidate_models
        chosen_slug = active_model

        def call_model(*, auth_key: str, model: str) -> GeminiCompletionResult:
            nonlocal chosen_slug
            completion = request_gemini_contents(
                auth_key=auth_key,
                model=model,
                contents=contents,
                system_instruction=system_prompt,
                tools=tools_payload,
            )
            has_tool_calls = bool(completion.function_calls)
            if not has_tool_calls and not completion.reply_text.strip():
                raise GeminiModelUnavailableError(
                    "Gemini API returned an empty chat answer.",
                )
            chosen_slug = model
            return completion

        try:
            completion = run_gemini_with_fallback(
                candidate_models=models_for_step,
                call_model=call_model,
            )
        except GeminiConfigurationError as error:
            return _build_failed_turn_update(
                messages=messages,
                turn_human_message_id=state["turn_human_message_id"],
                error_kind=ERROR_KIND_CONFIG,
                error_detail=str(error),
            )
        except GeminiAnalysisError as error:
            return _build_failed_turn_update(
                messages=messages,
                turn_human_message_id=state["turn_human_message_id"],
                error_kind=ERROR_KIND_ANALYSIS,
                error_detail=str(error),
            )

        if completion.function_calls:
            tool_calls = [
                {
                    "name": call.name,
                    "args": call.args,
                    "id": f"{TOOL_CALL_ID_PREFIX}{uuid4().hex[:12]}",
                    "type": "tool_call",
                }
                for call in completion.function_calls
            ]
            ai_message = AIMessage(
                content=completion.reply_text,
                tool_calls=tool_calls,
                additional_kwargs={
                    GEMINI_RAW_PARTS_KEY: list(completion.raw_parts),
                },
            )
            return {
                "messages": [ai_message],
                "active_model": chosen_slug,
                "reply_model": completion.model,
                "tool_rounds": tool_rounds + 1,
                "error_kind": "",
                "error_detail": "",
            }

        return {
            "messages": [AIMessage(content=completion.reply_text.strip())],
            "active_model": chosen_slug,
            "reply_model": completion.model,
            "error_kind": "",
            "error_detail": "",
        }

    def _run_tools_node(
        self,
        state: ChatGraphState,
    ) -> dict[str, object]:
        try:
            tool_result = self._tool_node.invoke({"messages": state["messages"]})
        except SQLAlchemyError as error:
            LOGGER.warning(
                "Database error while executing read-only chat tool",
                extra={"error_type": type(error).__name__},
            )
            return _build_failed_turn_update(
                messages=state["messages"],
                turn_human_message_id=state["turn_human_message_id"],
                error_kind=ERROR_KIND_DATABASE,
                error_detail=str(error),
            )

        return {
            "messages": tool_result["messages"],
            "error_kind": "",
            "error_detail": "",
        }


def _route_after_agent(state: ChatGraphState) -> str:
    if state.get("error_kind") or not state.get("messages"):
        return END
    return tools_condition(state)


def _route_after_tools(state: ChatGraphState) -> str:
    if state.get("error_kind"):
        return END
    return AGENT_NODE


def _raise_if_turn_failed(result_state: dict[str, object]) -> None:
    error_kind = str(result_state.get("error_kind", ""))
    error_detail = str(result_state.get("error_detail", ""))
    if error_kind == ERROR_KIND_CONFIG:
        raise GeminiConfigurationError(error_detail)
    if error_kind == ERROR_KIND_ANALYSIS:
        raise GeminiAnalysisError(error_detail)
    if error_kind == ERROR_KIND_DATABASE:
        raise SQLAlchemyError(error_detail)


def _build_thread_config(*, thread_id: str) -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": thread_id,
        },
    }


def _build_failed_turn_update(
    *,
    messages: list[AnyMessage],
    turn_human_message_id: str,
    error_kind: str,
    error_detail: str,
) -> dict[str, object]:
    # Roll back the entire incomplete turn (prompt, tool calls, tool responses).
    start_index = 0
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].id == turn_human_message_id:
            start_index = index
            break

    cleanup_messages: list[RemoveMessage] = [
        RemoveMessage(id=str(message.id))
        for message in messages[start_index:]
        if message.id
    ]
    return {
        "messages": cleanup_messages,
        "reply_model": "",
        "error_kind": error_kind,
        "error_detail": error_detail,
    }


def _slice_clean_history_window(
    *,
    messages: list[AnyMessage],
    max_messages: int,
) -> list[AnyMessage]:
    window = list(messages[-max_messages:])
    while window and not isinstance(window[0], HumanMessage):
        window.pop(0)
    return window or list(messages)


def _messages_to_gemini_contents(
    *,
    messages: list[AnyMessage],
) -> list[dict[str, object]]:
    contents: list[dict[str, object]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if isinstance(message, HumanMessage):
            content_text = str(message.content).strip()
            if content_text:
                contents.append(
                    {
                        "role": GEMINI_USER_ROLE,
                        "parts": [{"text": content_text}],
                    },
                )
            index += 1
            continue

        if isinstance(message, AIMessage):
            ai_parts = _ai_message_to_gemini_parts(message)
            if ai_parts:
                contents.append(
                    {
                        "role": GEMINI_MODEL_ROLE,
                        "parts": ai_parts,
                    },
                )
            index += 1
            continue

        if isinstance(message, ToolMessage):
            tool_parts: list[dict[str, object]] = []
            while index < len(messages) and isinstance(messages[index], ToolMessage):
                current_tool_msg = messages[index]
                assert isinstance(current_tool_msg, ToolMessage)
                tool_parts.append(_tool_message_to_gemini_part(current_tool_msg))
                index += 1
            if tool_parts:
                contents.append(
                    {
                        "role": GEMINI_USER_ROLE,
                        "parts": tool_parts,
                    },
                )
            continue

        index += 1
    return contents


def _ai_message_to_gemini_parts(message: AIMessage) -> list[dict[str, object]]:
    raw_parts = message.additional_kwargs.get(GEMINI_RAW_PARTS_KEY)
    if isinstance(raw_parts, list) and raw_parts:
        return [part for part in raw_parts if isinstance(part, dict)]

    parts: list[dict[str, object]] = []
    text_content = str(message.content).strip()
    if text_content:
        parts.append({"text": text_content})

    for tool_call in message.tool_calls:
        parts.append(
            {
                "functionCall": {
                    "name": tool_call["name"],
                    "args": tool_call.get("args", {}),
                },
            },
        )
    return parts


def _tool_message_to_gemini_part(message: ToolMessage) -> dict[str, object]:
    raw_content = str(message.content)
    try:
        parsed_output: object = json.loads(raw_content)
        response_payload = (
            parsed_output
            if isinstance(parsed_output, dict)
            else {"result": parsed_output}
        )
    except ValueError:
        response_payload = {"error": raw_content}

    return {
        "functionResponse": {
            "name": message.name or "",
            "response": response_payload,
        },
    }


def _extract_latest_assistant_answer(messages: list[AnyMessage]) -> str:
    if not messages or not isinstance(messages[-1], AIMessage):
        raise GeminiAnalysisError(
            "AI-асистент не повернув коректної відповіді.",
        )
    answer = str(messages[-1].content).strip()
    if not answer:
        raise GeminiAnalysisError(
            "AI-асистент повернув порожню відповідь.",
        )
    return answer
