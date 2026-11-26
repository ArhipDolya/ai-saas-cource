import json
import logging
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from typing import TypeVar

from dotenv import load_dotenv

from app.database import ENV_PATH
from app.expenses import TransactionRecord
from app.prompts import build_transaction_analysis_prompt


TResult = TypeVar("TResult")
GEMINI_API_KEY_VARIABLE = "GEMINI_API_KEY"
GEMINI_MODEL_VARIABLE = "GEMINI_MODEL"
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"  # Default fast Gemini Flash-Lite model.
GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_REQUEST_TIMEOUT_SECONDS = 25
GEMINI_JSON_MIME_TYPE = "application/json"
GEMINI_MODEL_PREFIX = "gemini-"
TRANSIENT_RETRY_LIMIT = 2
RETRY_BACKOFF_SECONDS = 1.5
ADVICE_MAX_COUNT = 3
MAX_TRANSACTIONS_FOR_ANALYSIS = 1000  # Avoid spending tokens on >1000 records.
FREE_GEMINI_MODELS: tuple[str, ...] = (
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.1-flash-lite-preview",
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-flash-latest",
    "gemma-4-26b-a4b-it",
    "gemma-4-31b-it",
)
FALLBACK_HTTP_STATUSES = frozenset(
    {
        HTTPStatus.BAD_REQUEST,
        HTTPStatus.NOT_FOUND,
        HTTPStatus.TOO_MANY_REQUESTS,
        HTTPStatus.INTERNAL_SERVER_ERROR,
        HTTPStatus.BAD_GATEWAY,
        HTTPStatus.SERVICE_UNAVAILABLE,
        HTTPStatus.GATEWAY_TIMEOUT,
    },
)
TRANSIENT_HTTP_STATUSES = frozenset(
    {
        HTTPStatus.TOO_MANY_REQUESTS,
        HTTPStatus.SERVICE_UNAVAILABLE,
        HTTPStatus.GATEWAY_TIMEOUT,
    },
)
LOGGER = logging.getLogger(__name__)


class GeminiConfigurationError(ValueError):
    """Gemini credentials are missing or invalid without exposing their values."""


class GeminiModelUnavailableError(RuntimeError):
    """A single Gemini model call failed or returned invalid structured output."""


class GeminiAnalysisError(RuntimeError):
    """All candidate free Gemini models failed to analyze transactions."""


class TransactionLimitExceededError(ValueError):
    """The number of transactions exceeds the safe token-budget limit."""


@dataclass(frozen=True, kw_only=True)
class GeminiFunctionCall:
    name: str
    args: dict[str, object]
    thought_signature: str | None = None


@dataclass(frozen=True, kw_only=True)
class GeminiCompletionResult:
    model: str
    reply_text: str
    function_calls: tuple[GeminiFunctionCall, ...] = ()
    raw_parts: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True, kw_only=True)
class TransactionAnalysisResult:
    summary: str
    top_expense_categories: list[str]
    risks: list[str]
    advice: list[str]
    model: str


def load_gemini_credentials() -> tuple[str, str | None]:
    """Read GEMINI_API_KEY and optional GEMINI_MODEL override from env or .env."""
    load_dotenv(dotenv_path=ENV_PATH)
    auth_key = os.getenv(GEMINI_API_KEY_VARIABLE, "").strip()
    if not auth_key:
        raise GeminiConfigurationError(
            "Set GEMINI_API_KEY in the environment or the project .env file.",
        )

    configured_model = os.getenv(GEMINI_MODEL_VARIABLE, "").strip() or None
    return auth_key, configured_model


def request_gemini_text(
    *,
    auth_key: str,
    model: str,
    prompt: str,
    expect_json: bool = False,
    timeout_seconds: int = GEMINI_REQUEST_TIMEOUT_SECONDS,
) -> GeminiCompletionResult:
    """Call Gemini generateContent for one prompt string and parse non-empty reply text."""
    contents: list[dict[str, object]] = [
        {
            "role": "user",
            "parts": [{"text": prompt}],
        },
    ]
    result = request_gemini_contents(
        auth_key=auth_key,
        model=model,
        contents=contents,
        expect_json=expect_json,
        timeout_seconds=timeout_seconds,
    )
    if not result.reply_text:
        raise GeminiModelUnavailableError(
            "Gemini API returned an empty text reply.",
        )
    return result


def request_gemini_contents(
    *,
    auth_key: str,
    model: str,
    contents: list[dict[str, object]],
    system_instruction: str | None = None,
    tools: list[dict[str, object]] | None = None,
    expect_json: bool = False,
    timeout_seconds: int = GEMINI_REQUEST_TIMEOUT_SECONDS,
) -> GeminiCompletionResult:
    """Call Gemini generateContent with multi-turn contents, tools, and system prompt."""
    payload = _build_gemini_payload(
        model=model,
        contents=contents,
        system_instruction=system_instruction,
        tools=tools,
        expect_json=expect_json,
    )
    endpoint = f"{GEMINI_API_BASE_URL}/{model}:generateContent"
    request_body = json.dumps(payload).encode("utf-8")
    response_payload = _post_json_with_transient_retries(
        endpoint=endpoint,
        auth_key=auth_key,
        request_body=request_body,
        timeout_seconds=timeout_seconds,
    )
    reply_text, function_calls, raw_parts = _extract_response_content(response_payload)
    resolved_model = (
        response_payload.get("modelVersion")
        if isinstance(response_payload, dict)
        else None
    )
    model_name = (
        resolved_model if isinstance(resolved_model, str) and resolved_model else model
    )
    return GeminiCompletionResult(
        model=model_name,
        reply_text=reply_text,
        function_calls=function_calls,
        raw_parts=raw_parts,
    )


def _build_gemini_payload(
    *,
    model: str,
    contents: list[dict[str, object]],
    system_instruction: str | None,
    tools: list[dict[str, object]] | None,
    expect_json: bool,
) -> dict[str, object]:
    is_gemini_family = model.startswith(GEMINI_MODEL_PREFIX)
    prepared_contents = list(contents)
    payload: dict[str, object] = {}

    if system_instruction:
        if is_gemini_family:
            payload["systemInstruction"] = {
                "parts": [{"text": system_instruction}],
            }
        else:
            prepared_contents = _inject_system_instruction_into_contents(
                contents=prepared_contents,
                system_instruction=system_instruction,
            )

    payload["contents"] = prepared_contents
    if tools:
        payload["tools"] = [{"functionDeclarations": tools}]
    if expect_json and is_gemini_family:
        payload["generationConfig"] = {
            "responseMimeType": GEMINI_JSON_MIME_TYPE,
        }
    return payload


def _inject_system_instruction_into_contents(
    *,
    contents: list[dict[str, object]],
    system_instruction: str,
) -> list[dict[str, object]]:
    # Non-Gemini models (for example Gemma fallbacks) may reject systemInstruction.
    if not contents:
        return [
            {
                "role": "user",
                "parts": [{"text": system_instruction}],
            },
        ]

    first_turn = contents[0]
    first_parts = first_turn.get("parts")
    is_user_turn = first_turn.get("role") == "user" and isinstance(first_parts, list)
    if is_user_turn:
        updated_first_turn = {
            **first_turn,
            "parts": [{"text": f"{system_instruction}\n\n"}, *first_parts],
        }
        return [updated_first_turn, *contents[1:]]

    return [
        {
            "role": "user",
            "parts": [{"text": system_instruction}],
        },
        *contents,
    ]


def _post_json_with_transient_retries(
    *,
    endpoint: str,
    auth_key: str,
    request_body: bytes,
    timeout_seconds: int,
) -> object:
    for attempt in range(1, TRANSIENT_RETRY_LIMIT + 1):
        request = urllib.request.Request(
            url=endpoint,
            data=request_body,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": auth_key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=timeout_seconds,
            ) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            error_detail = _extract_http_error_message(error)
            is_auth_error = error.code in {
                HTTPStatus.UNAUTHORIZED,
                HTTPStatus.FORBIDDEN,
            }
            if is_auth_error:
                raise GeminiConfigurationError(
                    f"Gemini API rejected authorization (HTTP {error.code}).",
                ) from None

            is_transient = (
                error.code in TRANSIENT_HTTP_STATUSES
                and attempt < TRANSIENT_RETRY_LIMIT
            )
            if is_transient:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue

            if error.code in FALLBACK_HTTP_STATUSES:
                raise GeminiModelUnavailableError(
                    f"HTTP {error.code}: {error_detail}",
                ) from None
            raise GeminiModelUnavailableError(
                f"HTTP {error.code}: {error_detail}",
            ) from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            is_transient = (
                isinstance(error, TimeoutError) and attempt < TRANSIENT_RETRY_LIMIT
            )
            if is_transient:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue
            raise GeminiModelUnavailableError(
                f"Network or decode error: {type(error).__name__}",
            ) from None

    raise GeminiModelUnavailableError("Exceeded retry limit for Gemini model.")


def _extract_http_error_message(error: urllib.error.HTTPError) -> str:
    try:
        raw_body = error.read().decode("utf-8", errors="replace")
        parsed: object = json.loads(raw_body)
    except (ValueError, OSError):
        return "Unexpected error response."

    if isinstance(parsed, dict):
        error_info = parsed.get("error")
        if isinstance(error_info, dict) and isinstance(
            error_info.get("message"),
            str,
        ):
            return error_info["message"]
    return "Unexpected error response."


def _extract_response_content(
    payload: object,
) -> tuple[str, tuple[GeminiFunctionCall, ...], tuple[dict[str, object], ...]]:
    if not isinstance(payload, dict):
        raise GeminiModelUnavailableError(
            "Gemini API returned an unexpected payload type.",
        )

    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise GeminiModelUnavailableError(
            "Gemini API returned no response candidates.",
        )

    first_candidate = candidates[0]
    content = (
        first_candidate.get("content") if isinstance(first_candidate, dict) else None
    )
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list) or not parts:
        raise GeminiModelUnavailableError(
            "Gemini API returned empty content parts.",
        )

    raw_parts: list[dict[str, object]] = []
    text_chunks: list[str] = []
    function_calls: list[GeminiFunctionCall] = []

    for part in parts:
        if not isinstance(part, dict):
            continue
        raw_parts.append(part)

        part_text = part.get("text")
        is_thought_part = bool(part.get("thought"))
        if isinstance(part_text, str) and not is_thought_part:
            text_chunks.append(part_text)

        raw_call = part.get("functionCall")
        if isinstance(raw_call, dict):
            call_name = raw_call.get("name")
            raw_args = raw_call.get("args")
            if isinstance(call_name, str) and call_name.strip():
                normalized_args: dict[str, object] = (
                    {str(k): v for k, v in raw_args.items()}
                    if isinstance(raw_args, dict)
                    else {}
                )
                thought_signature = part.get("thoughtSignature")
                function_calls.append(
                    GeminiFunctionCall(
                        name=call_name.strip(),
                        args=normalized_args,
                        thought_signature=(
                            thought_signature
                            if isinstance(thought_signature, str)
                            else None
                        ),
                    ),
                )

    reply_text = "".join(text_chunks).strip()
    if not reply_text and not function_calls:
        raise GeminiModelUnavailableError(
            "Gemini API returned an empty text reply.",
        )
    return reply_text, tuple(function_calls), tuple(raw_parts)


class TransactionAnalysisService:
    """Analyze persisted user transactions via Gemini with free-tier fallbacks."""

    def __init__(
        self,
        *,
        candidate_models: tuple[str, ...] = FREE_GEMINI_MODELS,
    ) -> None:
        self._candidate_models = candidate_models

    def analyze_transactions(
        self,
        *,
        transactions: list[TransactionRecord],
    ) -> TransactionAnalysisResult:
        """Build prompt from transactions and try free models until one succeeds."""
        if len(transactions) > MAX_TRANSACTIONS_FOR_ANALYSIS:
            raise TransactionLimitExceededError(
                "Занадто багато транзакцій для AI-аналізу "
                f"({len(transactions)} > {MAX_TRANSACTIONS_FOR_ANALYSIS}). "
                "Запит до моделі не виконано, щоб не витрачати зайві токени.",
            )

        serialized_transactions = serialize_transactions_for_prompt(transactions)
        prompt = build_transaction_analysis_prompt(serialized_transactions)

        def call_model(*, auth_key: str, model: str) -> TransactionAnalysisResult:
            completion = request_gemini_text(
                auth_key=auth_key,
                model=model,
                prompt=prompt,
                expect_json=True,
            )
            analysis = _parse_analysis_json(
                raw_text=completion.reply_text,
                model=completion.model,
            )
            LOGGER.info(
                "Transactions analyzed with Gemini",
                extra={
                    "model": analysis.model,
                    "transaction_count": len(transactions),
                },
            )
            return analysis

        return run_gemini_with_fallback(
            candidate_models=self._candidate_models,
            call_model=call_model,
        )


def resolve_candidate_models(
    *,
    preferred_model: str | None,
    candidate_models: tuple[str, ...],
) -> list[str]:
    """Order candidate free models with an explicit env override tried first."""
    if preferred_model is None:
        return list(candidate_models)
    remaining_models = [model for model in candidate_models if model != preferred_model]
    return [preferred_model, *remaining_models]


def run_gemini_with_fallback(
    *,
    candidate_models: tuple[str, ...],
    call_model: Callable[..., TResult],
) -> TResult:
    """Try candidate free models in priority order until one succeeds."""
    auth_key, preferred_model = load_gemini_credentials()
    models_to_try = resolve_candidate_models(
        preferred_model=preferred_model,
        candidate_models=candidate_models,
    )

    for model in models_to_try:
        try:
            return call_model(auth_key=auth_key, model=model)
        except GeminiModelUnavailableError as error:
            LOGGER.warning(
                "Gemini candidate model unavailable, trying next fallback",
                extra={
                    "model": model,
                    "reason": str(error)[:200],
                },
            )

    raise GeminiAnalysisError(
        "Усі безкоштовні LLM-моделі зараз недоступні. Спробуйте пізніше.",
    )


def serialize_transactions_for_prompt(
    transactions: list[TransactionRecord],
) -> str:
    """Serialize transaction records into the JSON structure expected by the prompt."""
    payload: list[dict[str, float | str | None]] = [
        transaction_to_prompt_dict(transaction) for transaction in transactions
    ]
    return json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
    )


def transaction_to_prompt_dict(
    transaction: TransactionRecord,
) -> dict[str, float | str | None]:
    """Convert one TransactionRecord into the LLM prompt JSON field structure."""
    return {
        "transaction_type": transaction.transaction_type,
        "amount": float(transaction.amount),
        "category_name": transaction.category_name,
        "description": transaction.description,
        "created_at": transaction.created_at.isoformat(),
    }


def _parse_analysis_json(
    *,
    raw_text: str,
    model: str,
) -> TransactionAnalysisResult:
    cleaned_text = _strip_markdown_fences(raw_text)
    try:
        parsed: object = json.loads(cleaned_text)
    except json.JSONDecodeError:
        raise GeminiModelUnavailableError(
            "Model returned non-JSON analysis output.",
        ) from None

    if not isinstance(parsed, dict):
        raise GeminiModelUnavailableError(
            "Model JSON root must be an object.",
        )

    summary = parsed.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise GeminiModelUnavailableError(
            "Model JSON is missing a non-empty 'summary' string.",
        )

    top_expense_categories = _validate_string_list(
        value=parsed.get("top_expense_categories"),
        field_name="top_expense_categories",
    )
    risks = _validate_string_list(
        value=parsed.get("risks"),
        field_name="risks",
    )
    advice = _validate_string_list(
        value=parsed.get("advice"),
        field_name="advice",
    )

    return TransactionAnalysisResult(
        summary=summary.strip(),
        top_expense_categories=top_expense_categories,
        risks=risks,
        advice=advice[:ADVICE_MAX_COUNT],
        model=model,
    )


def _strip_markdown_fences(raw_text: str) -> str:
    text = raw_text.strip()
    if not text.startswith("```"):
        return text

    lines = text.splitlines()
    if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return text


def _validate_string_list(
    *,
    value: object,
    field_name: str,
) -> list[str]:
    if not isinstance(value, list):
        raise GeminiModelUnavailableError(
            f"Model JSON field '{field_name}' must be a list.",
        )

    normalized_items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise GeminiModelUnavailableError(
                f"Model JSON field '{field_name}' must contain only strings.",
            )
        stripped = item.strip()
        if stripped:
            normalized_items.append(stripped)
    return normalized_items
