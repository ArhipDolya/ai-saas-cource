import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http import HTTPStatus

from dotenv import load_dotenv

from app.database import ENV_PATH
from app.expenses import TransactionRecord
from app.prompts import build_transaction_analysis_prompt


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
class GeminiCompletionResult:
    model: str
    reply_text: str


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
    """Call Gemini generateContent for one model and parse the reply text."""
    payload: dict[str, object] = {
        "contents": [
            {
                "parts": [{"text": prompt}],
            },
        ],
    }
    is_gemini_family = model.startswith(GEMINI_MODEL_PREFIX)
    if expect_json and is_gemini_family:
        payload["generationConfig"] = {
            "responseMimeType": GEMINI_JSON_MIME_TYPE,
        }

    endpoint = f"{GEMINI_API_BASE_URL}/{model}:generateContent"
    request_body = json.dumps(payload).encode("utf-8")
    response_payload = _post_json_with_transient_retries(
        endpoint=endpoint,
        auth_key=auth_key,
        request_body=request_body,
        timeout_seconds=timeout_seconds,
    )
    reply_text = _extract_reply_text(response_payload)
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
    )


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


def _extract_reply_text(payload: object) -> str:
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

    text_chunks = [
        part["text"]
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ]
    reply_text = "".join(text_chunks).strip()
    if not reply_text:
        raise GeminiModelUnavailableError(
            "Gemini API returned an empty text reply.",
        )
    return reply_text


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

        auth_key, preferred_model = load_gemini_credentials()
        serialized_transactions = serialize_transactions_for_prompt(transactions)
        prompt = build_transaction_analysis_prompt(serialized_transactions)
        models_to_try = self._resolve_model_order(preferred_model=preferred_model)

        failure_reasons: list[str] = []
        for model in models_to_try:
            try:
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
            except GeminiModelUnavailableError as error:
                LOGGER.warning(
                    "Gemini candidate model unavailable, trying next fallback",
                    extra={
                        "model": model,
                        "reason": str(error)[:200],
                    },
                )
                failure_reasons.append(f"{model}: {error}")

        raise GeminiAnalysisError(
            "Усі безкоштовні LLM-моделі зараз недоступні. Спробуйте пізніше.",
        )

    def _resolve_model_order(
        self,
        *,
        preferred_model: str | None,
    ) -> list[str]:
        if preferred_model is None:
            return list(self._candidate_models)
        remaining_models = [
            model for model in self._candidate_models if model != preferred_model
        ]
        return [preferred_model, *remaining_models]


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
