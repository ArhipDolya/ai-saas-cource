import io
import json
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime
from decimal import Decimal
from email.message import Message
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch

import psycopg
from aiohttp.test_utils import AioHTTPTestCase, TestClient, TestServer
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from sqlalchemy.exc import SQLAlchemyError

from app.ai import (
    DEFAULT_GEMINI_MODEL,
    GeminiAnalysisError,
    GeminiCompletionResult,
    GeminiConfigurationError,
    TransactionAnalysisService,
)
from app.api import (
    AI_ANALYZE_TRANSACTIONS_PATH,
    AI_CHAT_PATH,
    AI_CONFIG_ERROR_MESSAGE,
    CHAT_STORAGE_ERROR_MESSAGE,
    INVALID_JSON_MESSAGE,
    UNAUTHORIZED_MESSAGE,
    create_api_app,
)
from app.auth import AdminAuthService
from app.chat import (
    CHAT_MESSAGE_MAX_LENGTH,
    CHAT_THREAD_ID_MAX_LENGTH,
    GEMINI_MODEL_ROLE,
    GEMINI_RAW_PARTS_KEY,
    GEMINI_USER_ROLE,
    MAX_CHAT_HISTORY_MESSAGES,
    MAX_TOOL_ROUNDS,
    ChatValidationError,
    ProjectChatService,
    _extract_latest_assistant_answer,
    _messages_to_gemini_contents,
    _slice_clean_history_window,
    parse_chat_payload,
)
from app.database import (
    create_langgraph_postgres_pool,
    load_postgres_connection_string,
)
from app.expenses import FinancialSummary, TransactionRecord
from app.prompts import (
    PromptTemplateError,
    _load_prompt_template,
    build_transaction_analysis_prompt,
)
from tests.test_readonly_finance_tools import create_seeded_sqlite_engine


class FakeUrlopenResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> "FakeUrlopenResponse":
        return self

    def __exit__(
        self,
        exc_type: object,
        exc_val: object,
        exc_tb: object,
    ) -> bool:
        return False


class FakeExpenseService:
    def __init__(
        self,
        *,
        summary: FinancialSummary | None = None,
        transactions: list[TransactionRecord] | None = None,
        raise_on_fetch: Exception | None = None,
    ) -> None:
        self._summary = summary or FinancialSummary(
            total_income=Decimal("15000.00"),
            total_expense=Decimal("320.00"),
            balance=Decimal("14680.00"),
        )
        self._transactions = (
            transactions
            if transactions is not None
            else [
                TransactionRecord(
                    id=1,
                    user_id=1,
                    telegram_id=123,
                    category_id=2,
                    category_name="кава",
                    transaction_type="expense",
                    amount=Decimal("120.00"),
                    description="Лате",
                    created_at=datetime(2026, 11, 26, 10, 0, 0),
                ),
            ]
        )
        self._raise_on_fetch = raise_on_fetch

    def get_summary(self) -> FinancialSummary:
        if self._raise_on_fetch is not None:
            raise self._raise_on_fetch
        return self._summary

    def list_transactions(
        self,
        *,
        transaction_type: str | None = None,
    ) -> list[TransactionRecord]:
        if self._raise_on_fetch is not None:
            raise self._raise_on_fetch
        if transaction_type is None:
            return list(self._transactions)
        return [
            item
            for item in self._transactions
            if item.transaction_type == transaction_type
        ]

    def get_transactions_summary_for_period(self, *, period: str = "all") -> object:
        if self._raise_on_fetch is not None:
            raise self._raise_on_fetch
        from app.expenses import PeriodFinancialSummary

        return PeriodFinancialSummary(
            period=period,
            start_at=None,
            end_at=None,
            total_income=self._summary.total_income,
            total_expense=self._summary.total_expense,
            balance=self._summary.balance,
            income_count=1,
            expense_count=1,
            total_count=2,
        )

    def get_category_totals_for_period(self, *, period: str = "all") -> list[object]:
        if self._raise_on_fetch is not None:
            raise self._raise_on_fetch
        return []

    def get_top_expenses_for_period(
        self,
        *,
        period: str = "all",
        limit: int = 5,
    ) -> list[TransactionRecord]:
        if self._raise_on_fetch is not None:
            raise self._raise_on_fetch
        return list(self._transactions[:limit])


class FailingCheckpointer(InMemorySaver):
    def get_tuple(self, config: object) -> object:
        raise psycopg.OperationalError("Simulated Neon SSL connection closed")


class LangGraphChatAndToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sqlite_engine = create_seeded_sqlite_engine()
        from app.expenses import ExpenseService

        self.sql_expense_service = ExpenseService(self.sqlite_engine)

    def tearDown(self) -> None:
        self.sqlite_engine.dispose()

    def test_invariant_1_parse_chat_payload_enforces_contract(self) -> None:
        invalid_payloads: list[object] = [
            [],
            "text",
            {"message": "Привіт"},
            {"thread_id": 123, "message": "Привіт"},
            {"thread_id": "   ", "message": "Привіт"},
            {"thread_id": "bad id", "message": "Привіт"},
            {"thread_id": "bad\tid", "message": "Привіт"},
            {"thread_id": "a" * (CHAT_THREAD_ID_MAX_LENGTH + 1), "message": "Привіт"},
            {"thread_id": "thread-1"},
            {"thread_id": "thread-1", "message": 999},
            {"thread_id": "thread-1", "message": "   \n  "},
            {
                "thread_id": "thread-1",
                "message": "х" * (CHAT_MESSAGE_MAX_LENGTH + 1),
            },
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ChatValidationError):
                    parse_chat_payload(payload)

        valid = parse_chat_payload(
            {
                "thread_id": f"  {'t' * CHAT_THREAD_ID_MAX_LENGTH}  ",
                "message": f"  {'м' * CHAT_MESSAGE_MAX_LENGTH}  ",
            },
        )
        self.assertEqual(len(valid.thread_id), CHAT_THREAD_ID_MAX_LENGTH)
        self.assertEqual(len(valid.message), CHAT_MESSAGE_MAX_LENGTH)

    def test_invariant_2_short_term_memory_persists_across_restarts_and_isolates_threads(
        self,
    ) -> None:
        shared_checkpointer = InMemorySaver()
        first_service_instance = ProjectChatService(
            expense_service=self.sql_expense_service,
            checkpointer=shared_checkpointer,
            candidate_models=(DEFAULT_GEMINI_MODEL,),
        )
        restarted_service_instance = ProjectChatService(
            expense_service=self.sql_expense_service,
            checkpointer=shared_checkpointer,
            candidate_models=(DEFAULT_GEMINI_MODEL,),
        )
        self.assertEqual(
            first_service_instance.tool_names,
            ("get_transactions_summary", "get_category_totals", "get_top_expenses"),
        )
        captured_calls: list[dict[str, object]] = []

        def fake_request(
            *,
            auth_key: str,
            model: str,
            contents: list[dict[str, object]],
            system_instruction: str | None = None,
            tools: list[dict[str, object]] | None = None,
            expect_json: bool = False,
            timeout_seconds: int = 25,
        ) -> GeminiCompletionResult:
            captured_calls.append(
                {
                    "model": model,
                    "contents": contents,
                    "system_instruction": system_instruction,
                    "tools": tools,
                },
            )
            turn_number = len(captured_calls)
            return GeminiCompletionResult(
                model=model,
                reply_text=f"  Відповідь {turn_number}  ",
            )

        with (
            patch(
                "app.ai.load_gemini_credentials",
                return_value=("fake-key", None),
            ),
            patch(
                "app.chat.request_gemini_contents",
                side_effect=fake_request,
            ),
        ):
            first_reply = first_service_instance.answer_message(
                chat_input=parse_chat_payload(
                    {"thread_id": "thread-A", "message": "Що вміє цей проєкт?"},
                ),
            )
            # Simulate process restart with a fresh compiled graph sharing the checkpointer.
            second_reply = restarted_service_instance.answer_message(
                chat_input=parse_chat_payload(
                    {"thread_id": "thread-A", "message": "А як додати витрату в боті?"},
                ),
            )
            other_thread_reply = restarted_service_instance.answer_message(
                chat_input=parse_chat_payload(
                    {"thread_id": "thread-B", "message": "Привіт з іншого чату"},
                ),
            )

        self.assertEqual(first_reply.answer, "Відповідь 1")
        self.assertEqual(second_reply.answer, "Відповідь 2")
        self.assertEqual(other_thread_reply.thread_id, "thread-B")

        second_contents = captured_calls[1]["contents"]
        assert isinstance(second_contents, list)
        self.assertEqual(len(second_contents), 3)
        self.assertEqual(second_contents[0]["role"], GEMINI_USER_ROLE)
        self.assertEqual(
            second_contents[0]["parts"],
            [{"text": "Що вміє цей проєкт?"}],
        )
        self.assertEqual(second_contents[1]["role"], GEMINI_MODEL_ROLE)
        self.assertEqual(second_contents[1]["parts"], [{"text": "Відповідь 1"}])
        self.assertEqual(second_contents[2]["role"], GEMINI_USER_ROLE)
        self.assertEqual(
            second_contents[2]["parts"],
            [{"text": "А як додати витрату в боті?"}],
        )

        # Thread B must NOT contain Thread A messages.
        thread_b_contents = captured_calls[2]["contents"]
        assert isinstance(thread_b_contents, list)
        self.assertEqual(len(thread_b_contents), 1)

        sent_tools = captured_calls[0]["tools"]
        assert isinstance(sent_tools, list)
        self.assertEqual(
            [item["name"] for item in sent_tools],
            ["get_transactions_summary", "get_category_totals", "get_top_expenses"],
        )

    def test_langgraph_tool_node_executes_tools_preserves_thought_signature_and_recovers_from_bad_args(
        self,
    ) -> None:
        checkpointer = InMemorySaver()
        service = ProjectChatService(
            expense_service=self.sql_expense_service,
            checkpointer=checkpointer,
            candidate_models=(DEFAULT_GEMINI_MODEL,),
        )
        captured_payloads: list[dict[str, object]] = []

        def fake_urlopen(request: object, timeout: int = 25) -> FakeUrlopenResponse:
            body = json.loads(request.data.decode("utf-8"))  # type: ignore[attr-defined]
            captured_payloads.append(body)
            step = len(captured_payloads)

            if step == 1:
                # Model requests invalid period first to verify ToolNode error recovery.
                return FakeUrlopenResponse(
                    {
                        "modelVersion": DEFAULT_GEMINI_MODEL,
                        "candidates": [
                            {
                                "content": {
                                    "role": "model",
                                    "parts": [
                                        {
                                            "functionCall": {
                                                "name": "get_transactions_summary",
                                                "args": {"period": "invalid_period"},
                                            },
                                            "thoughtSignature": "sig-step-1",
                                        },
                                    ],
                                },
                            },
                        ],
                    },
                )
            if step == 2:
                # Model sees tool error and triggers two parallel read-only tool calls.
                return FakeUrlopenResponse(
                    {
                        "modelVersion": DEFAULT_GEMINI_MODEL,
                        "candidates": [
                            {
                                "content": {
                                    "role": "model",
                                    "parts": [
                                        {"thought": True, "text": "internal thought"},
                                        {
                                            "functionCall": {
                                                "name": "get_transactions_summary",
                                                "args": {"period": "2026-11"},
                                            },
                                            "thoughtSignature": "sig-step-2",
                                        },
                                        {
                                            "functionCall": {
                                                "name": "get_top_expenses",
                                                "args": {
                                                    "period": "2026-11",
                                                    "limit": 2,
                                                },
                                            },
                                        },
                                    ],
                                },
                            },
                        ],
                    },
                )
            return FakeUrlopenResponse(
                {
                    "modelVersion": DEFAULT_GEMINI_MODEL,
                    "candidates": [
                        {
                            "content": {
                                "role": "model",
                                "parts": [
                                    {"thought": True, "text": "hidden thinking"},
                                    {
                                        "text": "За листопад 2026 дохід 17000.00 грн, витрати 1150.00 грн; топ - паливо 950.00 грн.",
                                    },
                                ],
                            },
                        },
                    ],
                },
            )

        with (
            patch("app.ai.load_gemini_credentials", return_value=("fake-key", None)),
            patch("urllib.request.urlopen", side_effect=fake_urlopen),
        ):
            reply = service.answer_message(
                chat_input=parse_chat_payload(
                    {
                        "thread_id": "t-tools-flow",
                        "message": "Підсумок і топ-2 витрати за 2026-11?",
                    },
                ),
            )

        self.assertEqual(
            reply.answer,
            "За листопад 2026 дохід 17000.00 грн, витрати 1150.00 грн; топ - паливо 950.00 грн.",
        )
        self.assertEqual(len(captured_payloads), 3)

        # Verify step 2 preserved exact model part including thoughtSignature and returned tool error.
        step2_contents = captured_payloads[1]["contents"]
        assert isinstance(step2_contents, list)
        self.assertEqual(
            step2_contents[1]["parts"][0]["thoughtSignature"],  # type: ignore[index]
            "sig-step-1",
        )
        self.assertEqual(
            step2_contents[2]["role"],  # type: ignore[index]
            GEMINI_USER_ROLE,
        )
        self.assertIn(
            "functionResponse",
            step2_contents[2]["parts"][0],  # type: ignore[index]
        )

        # Verify step 3 grouped BOTH parallel functionResponse results into one turn and kept SQL values.
        step3_contents = captured_payloads[2]["contents"]
        assert isinstance(step3_contents, list)
        last_tool_turn = step3_contents[-1]
        self.assertEqual(len(last_tool_turn["parts"]), 2)  # type: ignore[arg-type]
        summary_resp = last_tool_turn["parts"][0]["functionResponse"]["response"]  # type: ignore[index]
        top_resp = last_tool_turn["parts"][1]["functionResponse"]["response"]  # type: ignore[index]
        self.assertEqual(summary_resp["total_income"], 17000.0)
        self.assertEqual(summary_resp["total_expense"], 1150.0)
        self.assertEqual(top_resp["expenses"][0]["category_name"], "паливо")
        self.assertEqual(top_resp["expenses"][0]["amount"], 950.0)

        # Stored thread messages preserve full history including ToolMessages.
        stored_messages = service.get_thread_messages(thread_id="t-tools-flow")
        self.assertTrue(any(isinstance(msg, ToolMessage) for msg in stored_messages))
        self.assertIsInstance(stored_messages[-1], AIMessage)

    def test_max_tool_rounds_disables_tools_for_final_turn(self) -> None:
        checkpointer = InMemorySaver()
        service = ProjectChatService(
            expense_service=self.sql_expense_service,
            checkpointer=checkpointer,
            candidate_models=(DEFAULT_GEMINI_MODEL,),
        )
        tools_arg_history: list[bool] = []

        def fake_request(
            *,
            auth_key: str,
            model: str,
            contents: list[dict[str, object]],
            system_instruction: str | None = None,
            tools: list[dict[str, object]] | None = None,
            expect_json: bool = False,
            timeout_seconds: int = 25,
        ) -> GeminiCompletionResult:
            tools_arg_history.append(tools is not None)
            if tools is not None:
                from app.ai import GeminiFunctionCall

                return GeminiCompletionResult(
                    model=model,
                    reply_text="",
                    function_calls=(
                        GeminiFunctionCall(
                            name="get_transactions_summary",
                            args={"period": "all"},
                        ),
                    ),
                    raw_parts=(
                        {
                            "functionCall": {
                                "name": "get_transactions_summary",
                                "args": {"period": "all"},
                            },
                        },
                    ),
                )
            return GeminiCompletionResult(model=model, reply_text="Підсумок готовий")

        with (
            patch("app.ai.load_gemini_credentials", return_value=("fake-key", None)),
            patch("app.chat.request_gemini_contents", side_effect=fake_request),
        ):
            reply = service.answer_message(
                chat_input=parse_chat_payload(
                    {"thread_id": "t-cap", "message": "Нескінченний запит"},
                ),
            )

        self.assertEqual(reply.answer, "Підсумок готовий")
        self.assertEqual(tools_arg_history, [*([True] * MAX_TOOL_ROUNDS), False])

    def test_invariant_3_failed_mid_tool_turn_rolls_back_all_turn_messages_atomically(
        self,
    ) -> None:
        checkpointer = InMemorySaver()
        service = ProjectChatService(
            expense_service=self.sql_expense_service,
            checkpointer=checkpointer,
            candidate_models=(DEFAULT_GEMINI_MODEL,),
        )
        call_count = 0

        def fake_request(
            *,
            auth_key: str,
            model: str,
            contents: list[dict[str, object]],
            system_instruction: str | None = None,
            tools: list[dict[str, object]] | None = None,
            expect_json: bool = False,
            timeout_seconds: int = 25,
        ) -> GeminiCompletionResult:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return GeminiCompletionResult(model=model, reply_text="Базовий діалог")
            if call_count == 2:
                from app.ai import GeminiFunctionCall

                return GeminiCompletionResult(
                    model=model,
                    reply_text="",
                    function_calls=(
                        GeminiFunctionCall(
                            name="get_transactions_summary",
                            args={"period": "2026-11"},
                        ),
                    ),
                    raw_parts=(
                        {
                            "functionCall": {
                                "name": "get_transactions_summary",
                                "args": {"period": "2026-11"},
                            },
                        },
                    ),
                )
            if call_count == 3:
                from app.ai import GeminiModelUnavailableError

                raise GeminiModelUnavailableError("503 during post-tool reply")
            return GeminiCompletionResult(model=model, reply_text="Успіх після повтору")

        with (
            patch("app.ai.load_gemini_credentials", return_value=("fake-key", None)),
            patch("app.chat.request_gemini_contents", side_effect=fake_request),
        ):
            first = service.answer_message(
                chat_input=parse_chat_payload(
                    {"thread_id": "t-mid-fail", "message": "Перше питання"},
                ),
            )
            self.assertEqual(first.answer, "Базовий діалог")

            with self.assertRaises(GeminiAnalysisError):
                service.answer_message(
                    chat_input=parse_chat_payload(
                        {"thread_id": "t-mid-fail", "message": "Питання зі збоєм"},
                    ),
                )

            # Previous successful turn stays intact; failed prompt + tool calls + ToolMessage are gone!
            saved_after_fail = service.get_thread_messages(thread_id="t-mid-fail")
            self.assertEqual(
                [msg.content for msg in saved_after_fail],
                ["Перше питання", "Базовий діалог"],
            )

            retry = service.answer_message(
                chat_input=parse_chat_payload(
                    {"thread_id": "t-mid-fail", "message": "Питання зі збоєм"},
                ),
            )
            self.assertEqual(retry.answer, "Успіх після повтору")

    def test_sliding_window_and_message_serialization_helpers(self) -> None:
        checkpointer = InMemorySaver()
        service = ProjectChatService(
            expense_service=self.sql_expense_service,
            checkpointer=checkpointer,
            candidate_models=(DEFAULT_GEMINI_MODEL,),
        )
        expense_lengths: list[int] = []

        def fake_request(
            *,
            auth_key: str,
            model: str,
            contents: list[dict[str, object]],
            system_instruction: str | None = None,
            tools: list[dict[str, object]] | None = None,
            expect_json: bool = False,
            timeout_seconds: int = 25,
        ) -> GeminiCompletionResult:
            expense_lengths.append(len(contents))
            return GeminiCompletionResult(model=model, reply_text="Ок")

        with (
            patch("app.ai.load_gemini_credentials", return_value=("fake-key", None)),
            patch("app.chat.request_gemini_contents", side_effect=fake_request),
        ):
            total_turns = (MAX_CHAT_HISTORY_MESSAGES // 2) + 3
            for turn in range(total_turns):
                service.answer_message(
                    chat_input=parse_chat_payload(
                        {"thread_id": "t-win", "message": f"Питання {turn}"},
                    ),
                )
        # Last 20 messages start on an AIMessage, so _slice_clean_history_window trims to 19 starting on HumanMessage.
        self.assertEqual(expense_lengths[-1], MAX_CHAT_HISTORY_MESSAGES - 1)

        # Sliding window must trim leading ToolMessage/AIMessage until a HumanMessage starts the slice.
        mixed_history = [
            HumanMessage(content="q1"),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "get_transactions_summary", "args": {}, "id": "c1"}
                ],
            ),
            ToolMessage(
                content='{"total": 100}',
                name="get_transactions_summary",
                tool_call_id="c1",
            ),
            AIMessage(content="a1"),
            HumanMessage(content="q2"),
            AIMessage(content="a2"),
        ]
        sliced = _slice_clean_history_window(messages=mixed_history, max_messages=4)
        self.assertIsInstance(sliced[0], HumanMessage)
        self.assertEqual(sliced[0].content, "q2")

        # Verify fallback AI serialization without raw_parts preserves text/functionCall & skips empty system.
        serialized = _messages_to_gemini_contents(
            messages=[
                SystemMessage(content="ignore"),
                HumanMessage(content="   "),
                HumanMessage(content="Запит"),
                AIMessage(
                    content="Виклик",
                    tool_calls=[
                        {
                            "name": "get_category_totals",
                            "args": {"period": "all"},
                            "id": "c2",
                        }
                    ],
                ),
                ToolMessage(
                    content="not-json", name="get_category_totals", tool_call_id="c2"
                ),
            ],
        )
        self.assertEqual(
            serialized[0], {"role": GEMINI_USER_ROLE, "parts": [{"text": "Запит"}]}
        )
        self.assertEqual(serialized[1]["role"], GEMINI_MODEL_ROLE)
        self.assertEqual(
            serialized[2]["parts"][0]["functionResponse"]["response"],  # type: ignore[index]
            {"error": "not-json"},
        )
        self.assertEqual(
            AIMessage(content="", additional_kwargs={GEMINI_RAW_PARTS_KEY: []}).content,
            "",
        )

    def test_empty_reply_triggers_model_fallback_and_prompt_validation(self) -> None:
        service = ProjectChatService(
            expense_service=self.sql_expense_service,
            checkpointer=InMemorySaver(),
            candidate_models=(DEFAULT_GEMINI_MODEL, "gemma-4-26b-a4b-it"),
        )

        def side_effect(*, model: str, **kwargs: object) -> GeminiCompletionResult:
            if model == DEFAULT_GEMINI_MODEL:
                return GeminiCompletionResult(model=model, reply_text="   ")
            return GeminiCompletionResult(model=model, reply_text="Резервна відповідь")

        with (
            patch("app.ai.load_gemini_credentials", return_value=("fake-key", None)),
            patch("app.chat.request_gemini_contents", side_effect=side_effect),
        ):
            reply = service.answer_message(
                chat_input=parse_chat_payload(
                    {"thread_id": "t-empty", "message": "Привіт"},
                ),
            )
        self.assertEqual(reply.answer, "Резервна відповідь")
        self.assertEqual(reply.model, "gemma-4-26b-a4b-it")

        with self.assertRaises(GeminiAnalysisError):
            _extract_latest_assistant_answer([])
        with self.assertRaises(GeminiAnalysisError):
            _extract_latest_assistant_answer([AIMessage(content="   ")])

        analyze_prompt = build_transaction_analysis_prompt("[]")
        self.assertIn("[]", analyze_prompt)

        with tempfile.TemporaryDirectory() as temp_dir:
            missing_file = Path(temp_dir) / "missing.txt"
            with self.assertRaises(PromptTemplateError):
                _load_prompt_template(
                    path=missing_file,
                    required_markers=("__CURRENT_DATE__",),
                )
            incomplete_file = Path(temp_dir) / "incomplete.txt"
            incomplete_file.write_text("Без маркера", encoding="utf-8")
            with self.assertRaises(PromptTemplateError):
                _load_prompt_template(
                    path=incomplete_file,
                    required_markers=("__CURRENT_DATE__",),
                )

    def test_invariant_4_neon_postgres_pool_configuration(self) -> None:
        with (
            patch("app.database.load_dotenv"),
            patch.dict(
                os.environ,
                {
                    "DATABASE_URL": "postgresql+psycopg://user:pass@ep-demo.neon.tech/neondb?sslmode=require",
                },
                clear=True,
            ),
        ):
            conninfo = load_postgres_connection_string()
            self.assertTrue(conninfo.startswith("postgresql://"))
            self.assertIn("sslmode=require", conninfo)

            pool = create_langgraph_postgres_pool()
            try:
                self.assertIsInstance(pool, ConnectionPool)
                self.assertEqual(pool.min_size, 1)
                self.assertEqual(pool.max_size, 5)
                self.assertIs(pool._check, ConnectionPool.check_connection)
                self.assertEqual(pool.kwargs.get("autocommit"), True)
                self.assertEqual(pool.kwargs.get("prepare_threshold"), 0)
                self.assertEqual(pool.kwargs.get("row_factory"), dict_row)
            finally:
                pool.close()


class ChatEndpointTests(AioHTTPTestCase):
    async def get_application(self):  # type: ignore[override]
        self.auth_service = AdminAuthService(admin_password="secret-password")
        self.token = self.auth_service.issue_token()
        self.expense_service = FakeExpenseService()
        self.checkpointer = InMemorySaver()
        self.chat_service = ProjectChatService(
            expense_service=self.expense_service,  # type: ignore[arg-type]
            checkpointer=self.checkpointer,
            candidate_models=(DEFAULT_GEMINI_MODEL, "gemma-4-26b-a4b-it"),
        )
        self.analysis_service = TransactionAnalysisService(
            candidate_models=(DEFAULT_GEMINI_MODEL,),
        )
        return create_api_app(
            expense_service=self.expense_service,  # type: ignore[arg-type]
            auth_service=self.auth_service,
            analysis_service=self.analysis_service,
            chat_service=self.chat_service,
        )

    async def test_requires_auth_and_allows_cors_preflight(self) -> None:
        unauth_resp = await self.client.post(
            AI_CHAT_PATH,
            json={"thread_id": "t-1", "message": "Привіт"},
        )
        self.assertEqual(unauth_resp.status, HTTPStatus.UNAUTHORIZED)
        body = await unauth_resp.json()
        self.assertEqual(body["detail"], UNAUTHORIZED_MESSAGE)

        options_resp = await self.client.options(AI_CHAT_PATH)
        self.assertEqual(options_resp.status, HTTPStatus.NO_CONTENT)
        self.assertIn("POST", options_resp.headers["Access-Control-Allow-Methods"])

    async def test_rejects_invalid_json_and_validation_errors(self) -> None:
        headers = {"Authorization": f"Bearer {self.token}"}

        bad_json_resp = await self.client.post(
            AI_CHAT_PATH,
            headers={**headers, "Content-Type": "application/json"},
            data="{broken-json",
        )
        self.assertEqual(bad_json_resp.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual((await bad_json_resp.json())["detail"], INVALID_JSON_MESSAGE)

        empty_msg_resp = await self.client.post(
            AI_CHAT_PATH,
            headers=headers,
            json={"thread_id": "thread-1", "message": "   "},
        )
        self.assertEqual(empty_msg_resp.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(
            self.chat_service.get_thread_messages(thread_id="thread-1"),
            [],
        )

    async def test_successful_chat_returns_answer_and_thread_id(self) -> None:
        headers = {"Authorization": f"Bearer {self.token}"}
        fake_response = FakeUrlopenResponse(
            {
                "modelVersion": DEFAULT_GEMINI_MODEL,
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": "У вас витрат на 320.00 грн, дохід 15000.00 грн.",
                                },
                            ],
                        },
                    },
                ],
            },
        )

        with (
            patch(
                "app.ai.load_gemini_credentials",
                return_value=("fake-key", None),
            ),
            patch("urllib.request.urlopen", return_value=fake_response),
        ):
            resp = await self.client.post(
                AI_CHAT_PATH,
                headers=headers,
                json={
                    "thread_id": "  thread-ui-1 ",
                    "message": "  Скільки я витратив? ",
                },
            )

        self.assertEqual(resp.status, HTTPStatus.OK)
        data = await resp.json()
        self.assertEqual(
            data,
            {
                "answer": "У вас витрат на 320.00 грн, дохід 15000.00 грн.",
                "thread_id": "thread-ui-1",
            },
        )

    async def test_model_failure_returns_503_and_leaves_no_orphan_prompt(self) -> None:
        headers = {"Authorization": f"Bearer {self.token}"}

        def always_fail(request: object, timeout: int = 25) -> object:
            raise urllib.error.HTTPError(
                url=str(request.full_url),  # type: ignore[attr-defined]
                code=HTTPStatus.NOT_FOUND,
                msg="Not Found",
                hdrs=Message(),
                fp=io.BytesIO(json.dumps({"error": {"message": "Missing"}}).encode()),
            )

        with (
            patch(
                "app.ai.load_gemini_credentials",
                return_value=("fake-key", None),
            ),
            patch("urllib.request.urlopen", side_effect=always_fail),
        ):
            resp = await self.client.post(
                AI_CHAT_PATH,
                headers=headers,
                json={"thread_id": "thread-fail", "message": "Тест"},
            )

        self.assertEqual(resp.status, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertEqual(
            self.chat_service.get_thread_messages(thread_id="thread-fail"),
            [],
        )

    async def test_error_mapping_for_config_db_tool_checkpointer_and_prompt_failures(
        self,
    ) -> None:
        headers = {"Authorization": f"Bearer {self.token}"}

        with patch(
            "app.ai.load_gemini_credentials",
            side_effect=GeminiConfigurationError("Missing key"),
        ):
            config_resp = await self.client.post(
                AI_CHAT_PATH,
                headers=headers,
                json={"thread_id": "t-err", "message": "Тест"},
            )
        self.assertEqual(config_resp.status, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertEqual((await config_resp.json())["detail"], AI_CONFIG_ERROR_MESSAGE)

        with patch(
            "app.chat.build_project_chat_system_prompt",
            side_effect=PromptTemplateError("Missing prompt file"),
        ):
            prompt_resp = await self.client.post(
                AI_CHAT_PATH,
                headers=headers,
                json={"thread_id": "t-err", "message": "Тест"},
            )
        self.assertEqual(prompt_resp.status, HTTPStatus.INTERNAL_SERVER_ERROR)

        # SQLAlchemy error triggered inside ToolNode during get_transactions_summary -> 500 + clean rollback.
        self.expense_service._raise_on_fetch = SQLAlchemyError("Neon read failed")
        tool_call_resp = FakeUrlopenResponse(
            {
                "modelVersion": DEFAULT_GEMINI_MODEL,
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "functionCall": {
                                        "name": "get_transactions_summary",
                                        "args": {"period": "all"},
                                    },
                                },
                            ],
                        },
                    },
                ],
            },
        )
        with (
            patch(
                "app.ai.load_gemini_credentials",
                return_value=("fake-key", None),
            ),
            patch("urllib.request.urlopen", return_value=tool_call_resp),
        ):
            db_resp = await self.client.post(
                AI_CHAT_PATH,
                headers=headers,
                json={"thread_id": "t-tool-db-err", "message": "Підсумок"},
            )
        self.assertEqual(db_resp.status, HTTPStatus.INTERNAL_SERVER_ERROR)
        self.assertEqual((await db_resp.json())["detail"], CHAT_STORAGE_ERROR_MESSAGE)
        self.assertEqual(
            self.chat_service.get_thread_messages(thread_id="t-tool-db-err"),
            [],
        )
        self.expense_service._raise_on_fetch = None

        # Psycopg / Neon error inside LangGraph checkpointer -> 500 CHAT_STORAGE_ERROR_MESSAGE.
        failing_app = create_api_app(
            expense_service=self.expense_service,  # type: ignore[arg-type]
            auth_service=self.auth_service,
            chat_service=ProjectChatService(
                expense_service=self.expense_service,  # type: ignore[arg-type]
                checkpointer=FailingCheckpointer(),
                candidate_models=(DEFAULT_GEMINI_MODEL,),
            ),
        )
        async with TestClient(TestServer(failing_app)) as temp_client:
            persist_err_resp = await temp_client.post(
                AI_CHAT_PATH,
                headers=headers,
                json={"thread_id": "t-db-err", "message": "Тест"},
            )
            persist_status = persist_err_resp.status
            persist_body = await persist_err_resp.json()
        self.assertEqual(persist_status, HTTPStatus.INTERNAL_SERVER_ERROR)
        self.assertEqual(persist_body["detail"], CHAT_STORAGE_ERROR_MESSAGE)

    async def test_existing_analyze_transactions_endpoint_keeps_working(self) -> None:
        headers = {"Authorization": f"Bearer {self.token}"}
        fake_response = FakeUrlopenResponse(
            {
                "modelVersion": DEFAULT_GEMINI_MODEL,
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        {
                                            "summary": "Баланс позитивний",
                                            "top_expense_categories": ["кава"],
                                            "risks": [],
                                            "advice": ["Тримайте бюджет"],
                                        },
                                    ),
                                },
                            ],
                        },
                    },
                ],
            },
        )
        with (
            patch(
                "app.ai.load_gemini_credentials",
                return_value=("fake-key", None),
            ),
            patch("urllib.request.urlopen", return_value=fake_response),
        ):
            resp = await self.client.post(AI_ANALYZE_TRANSACTIONS_PATH, headers=headers)
        self.assertEqual(resp.status, HTTPStatus.OK)
        payload = await resp.json()
        self.assertEqual(payload["summary"], "Баланс позитивний")
