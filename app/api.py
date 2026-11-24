import asyncio
import logging
import os
from http import HTTPMethod, HTTPStatus

from aiohttp import web
from aiohttp.typedefs import Handler
from sqlalchemy.exc import SQLAlchemyError

from app.ai import (
    GeminiAnalysisError,
    GeminiConfigurationError,
    TransactionAnalysisResult,
    TransactionAnalysisService,
    TransactionLimitExceededError,
)
from app.auth import TOKEN_TTL_SECONDS, AdminAuthService, extract_bearer_token
from app.expenses import (
    CreateTransactionInput,
    DeletedTransaction,
    ExpenseService,
    FinancialSummary,
    TransactionConflictError,
    TransactionNotFoundError,
    TransactionRecord,
    TransactionValidationError,
    parse_optional_transaction_type,
    parse_transaction_filter,
    parse_transaction_id,
    parse_transaction_payload,
)
from app.prompts import PromptTemplateError


AUTH_LOGIN_PATH = "/api/auth/login"
AUTH_SESSION_PATH = "/api/auth/session"
TRANSACTIONS_PATH = "/api/transactions"
TRANSACTION_ITEM_PATH = "/api/transactions/{id}"
SUMMARY_PATH = "/api/summary"
AI_ANALYZE_TRANSACTIONS_PATH = "/api/ai/analyze-transactions"
DEFAULT_API_HOST = "0.0.0.0"  # Bind all interfaces for local and Docker access
DEFAULT_API_PORT = 8000  # Default HTTP port for backend API
API_HOST_VARIABLE = "API_HOST"
API_PORT_VARIABLE = "API_PORT"
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
}
INVALID_JSON_MESSAGE = "Некоректний JSON у тілі запиту."
PASSWORD_REQUIRED_MESSAGE = "Введіть пароль адміністратора."
INVALID_PASSWORD_MESSAGE = "Невірний пароль адміністратора."
UNAUTHORIZED_MESSAGE = "Потрібна авторизація. Введіть пароль адміністратора."
AI_CONFIG_ERROR_MESSAGE = (
    "AI-аналіз недоступний: перевірте налаштування GEMINI_API_KEY."
)
CREATE_TRANSACTION_ERROR_MESSAGE = "Failed to save transaction."
DELETE_TRANSACTION_ERROR_MESSAGE = "Failed to delete transaction."
DATABASE_ERROR_MESSAGE = "Failed to fetch transactions."
SUMMARY_ERROR_MESSAGE = "Failed to fetch summary."
EXPENSE_SERVICE_KEY: web.AppKey[ExpenseService] = web.AppKey(
    "expense_service",
    ExpenseService,
)
AUTH_SERVICE_KEY: web.AppKey[AdminAuthService] = web.AppKey(
    "auth_service",
    AdminAuthService,
)
ANALYSIS_SERVICE_KEY: web.AppKey[TransactionAnalysisService] = web.AppKey(
    "analysis_service",
    TransactionAnalysisService,
)
PUBLIC_PATHS = frozenset({AUTH_LOGIN_PATH})
LOGGER = logging.getLogger(__name__)


async def start_api_server(
    *,
    expense_service: ExpenseService,
    auth_service: AdminAuthService,
    analysis_service: TransactionAnalysisService | None = None,
    host: str = DEFAULT_API_HOST,
    port: int = DEFAULT_API_PORT,
) -> web.AppRunner:
    app = create_api_app(
        expense_service=expense_service,
        auth_service=auth_service,
        analysis_service=analysis_service,
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(
        runner=runner,
        host=host,
        port=port,
    )
    await site.start()
    LOGGER.info(
        "API server started",
        extra={"host": host, "port": port},
    )
    return runner


def create_api_app(
    *,
    expense_service: ExpenseService,
    auth_service: AdminAuthService,
    analysis_service: TransactionAnalysisService | None = None,
) -> web.Application:
    app = web.Application(middlewares=[require_admin_auth_middleware])
    app[EXPENSE_SERVICE_KEY] = expense_service
    app[AUTH_SERVICE_KEY] = auth_service
    app[ANALYSIS_SERVICE_KEY] = analysis_service or TransactionAnalysisService()
    app.router.add_post(AUTH_LOGIN_PATH, handle_login)
    app.router.add_options(AUTH_LOGIN_PATH, handle_cors_preflight)
    app.router.add_get(AUTH_SESSION_PATH, handle_session_check)
    app.router.add_options(AUTH_SESSION_PATH, handle_cors_preflight)
    app.router.add_get(TRANSACTIONS_PATH, handle_get_transactions)
    app.router.add_post(TRANSACTIONS_PATH, handle_create_transaction)
    app.router.add_options(TRANSACTIONS_PATH, handle_cors_preflight)
    app.router.add_delete(TRANSACTION_ITEM_PATH, handle_delete_transaction)
    app.router.add_options(TRANSACTION_ITEM_PATH, handle_cors_preflight)
    app.router.add_get(SUMMARY_PATH, handle_get_summary)
    app.router.add_options(SUMMARY_PATH, handle_cors_preflight)
    app.router.add_post(
        AI_ANALYZE_TRANSACTIONS_PATH,
        handle_analyze_transactions,
    )
    app.router.add_options(
        AI_ANALYZE_TRANSACTIONS_PATH,
        handle_cors_preflight,
    )
    return app


@web.middleware
async def require_admin_auth_middleware(
    request: web.Request,
    handler: Handler,
) -> web.StreamResponse:
    is_preflight = request.method == HTTPMethod.OPTIONS
    is_public_route = request.path in PUBLIC_PATHS
    if is_preflight or is_public_route:
        return await handler(request)

    token = extract_bearer_token(request.headers.get("Authorization"))
    auth_service = request.app[AUTH_SERVICE_KEY]
    if token is None or not auth_service.verify_token(token):
        return web.json_response(
            data={"detail": UNAUTHORIZED_MESSAGE},
            status=HTTPStatus.UNAUTHORIZED,
            headers=CORS_HEADERS,
        )

    return await handler(request)


async def handle_login(request: web.Request) -> web.Response:
    try:
        raw_payload: object = await request.json()
    except ValueError:
        return web.json_response(
            data={"detail": INVALID_JSON_MESSAGE},
            status=HTTPStatus.BAD_REQUEST,
            headers=CORS_HEADERS,
        )

    if not isinstance(raw_payload, dict):
        return web.json_response(
            data={"detail": PASSWORD_REQUIRED_MESSAGE},
            status=HTTPStatus.BAD_REQUEST,
            headers=CORS_HEADERS,
        )

    raw_password = raw_payload.get("password")
    if not isinstance(raw_password, str) or not raw_password:
        return web.json_response(
            data={"detail": PASSWORD_REQUIRED_MESSAGE},
            status=HTTPStatus.BAD_REQUEST,
            headers=CORS_HEADERS,
        )

    auth_service = request.app[AUTH_SERVICE_KEY]
    if not auth_service.verify_password(raw_password):
        LOGGER.warning("Admin login rejected: invalid password")
        return web.json_response(
            data={"detail": INVALID_PASSWORD_MESSAGE},
            status=HTTPStatus.UNAUTHORIZED,
            headers=CORS_HEADERS,
        )

    return web.json_response(
        data={
            "token": auth_service.issue_token(),
            "expires_in": TOKEN_TTL_SECONDS,
        },
        status=HTTPStatus.OK,
        headers=CORS_HEADERS,
    )


async def handle_session_check(request: web.Request) -> web.Response:
    return web.json_response(
        data={"authenticated": True},
        status=HTTPStatus.OK,
        headers=CORS_HEADERS,
    )


async def handle_cors_preflight(request: web.Request) -> web.Response:
    return web.Response(
        status=HTTPStatus.NO_CONTENT,
        headers=CORS_HEADERS,
    )


async def handle_get_transactions(request: web.Request) -> web.Response:
    raw_type = request.query.get("type")
    try:
        transaction_type = parse_transaction_filter(raw_type)
    except TransactionValidationError as error:
        return web.json_response(
            data={"detail": str(error)},
            status=HTTPStatus.BAD_REQUEST,
            headers=CORS_HEADERS,
        )

    expense_service = request.app[EXPENSE_SERVICE_KEY]
    try:
        transactions: list[TransactionRecord] = await asyncio.to_thread(
            expense_service.list_transactions,
            transaction_type=transaction_type,
        )
    except SQLAlchemyError as error:
        LOGGER.warning(
            "Transactions fetch failed",
            extra={"error_type": type(error).__name__},
        )
        return web.json_response(
            data={"detail": DATABASE_ERROR_MESSAGE},
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
            headers=CORS_HEADERS,
        )

    payload: list[dict[str, int | float | str | None]] = [
        _serialize_transaction(transaction) for transaction in transactions
    ]
    return web.json_response(
        data=payload,
        status=HTTPStatus.OK,
        headers=CORS_HEADERS,
    )


async def handle_create_transaction(request: web.Request) -> web.Response:
    try:
        raw_payload: object = await request.json()
    except ValueError:
        return web.json_response(
            data={"detail": INVALID_JSON_MESSAGE},
            status=HTTPStatus.BAD_REQUEST,
            headers=CORS_HEADERS,
        )

    try:
        transaction_input: CreateTransactionInput = parse_transaction_payload(
            raw_payload,
        )
    except TransactionValidationError as error:
        return web.json_response(
            data={"detail": str(error)},
            status=HTTPStatus.BAD_REQUEST,
            headers=CORS_HEADERS,
        )

    expense_service = request.app[EXPENSE_SERVICE_KEY]
    try:
        created_transaction: TransactionRecord = await asyncio.to_thread(
            expense_service.create_transaction,
            transaction=transaction_input,
        )
    except SQLAlchemyError as error:
        LOGGER.warning(
            "Transaction creation failed",
            extra={"error_type": type(error).__name__},
        )
        return web.json_response(
            data={"detail": CREATE_TRANSACTION_ERROR_MESSAGE},
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
            headers=CORS_HEADERS,
        )

    return web.json_response(
        data=_serialize_transaction(created_transaction),
        status=HTTPStatus.CREATED,
        headers=CORS_HEADERS,
    )


async def handle_delete_transaction(request: web.Request) -> web.Response:
    raw_id = request.match_info.get("id")
    raw_type = request.query.get("type")
    try:
        transaction_id = parse_transaction_id(raw_id)
        transaction_type = parse_optional_transaction_type(raw_type)
    except TransactionValidationError as error:
        return web.json_response(
            data={"detail": str(error)},
            status=HTTPStatus.BAD_REQUEST,
            headers=CORS_HEADERS,
        )

    expense_service = request.app[EXPENSE_SERVICE_KEY]
    try:
        deleted_transaction: DeletedTransaction = await asyncio.to_thread(
            expense_service.delete_transaction,
            transaction_id=transaction_id,
            transaction_type=transaction_type,
        )
    except TransactionNotFoundError as error:
        return web.json_response(
            data={"detail": str(error)},
            status=HTTPStatus.NOT_FOUND,
            headers=CORS_HEADERS,
        )
    except TransactionConflictError as error:
        return web.json_response(
            data={"detail": str(error)},
            status=HTTPStatus.CONFLICT,
            headers=CORS_HEADERS,
        )
    except SQLAlchemyError as error:
        LOGGER.warning(
            "Transaction deletion failed",
            extra={
                "transaction_id": transaction_id,
                "error_type": type(error).__name__,
            },
        )
        return web.json_response(
            data={"detail": DELETE_TRANSACTION_ERROR_MESSAGE},
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
            headers=CORS_HEADERS,
        )

    return web.json_response(
        data={
            "deleted_id": deleted_transaction.id,
            "transaction_type": deleted_transaction.transaction_type,
        },
        status=HTTPStatus.OK,
        headers=CORS_HEADERS,
    )


def _serialize_transaction(
    transaction: TransactionRecord,
) -> dict[str, int | float | str | None]:
    return {
        "id": transaction.id,
        "user_id": transaction.user_id,
        "telegram_id": transaction.telegram_id,
        "category_id": transaction.category_id,
        "category_name": transaction.category_name,
        "transaction_type": transaction.transaction_type,
        "amount": float(transaction.amount),
        "description": transaction.description,
        "created_at": transaction.created_at.isoformat(),
    }


async def handle_get_summary(request: web.Request) -> web.Response:
    expense_service = request.app[EXPENSE_SERVICE_KEY]
    try:
        summary: FinancialSummary = await asyncio.to_thread(
            expense_service.get_summary,
        )
    except SQLAlchemyError as error:
        LOGGER.warning(
            "Summary fetch failed",
            extra={"error_type": type(error).__name__},
        )
        return web.json_response(
            data={"detail": SUMMARY_ERROR_MESSAGE},
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
            headers=CORS_HEADERS,
        )

    return web.json_response(
        data=_serialize_summary(summary),
        status=HTTPStatus.OK,
        headers=CORS_HEADERS,
    )


def _serialize_summary(summary: FinancialSummary) -> dict[str, float]:
    return {
        "total_income": float(summary.total_income),
        "total_expense": float(summary.total_expense),
        "balance": float(summary.balance),
    }


async def handle_analyze_transactions(request: web.Request) -> web.Response:
    expense_service = request.app[EXPENSE_SERVICE_KEY]
    try:
        transactions: list[TransactionRecord] = await asyncio.to_thread(
            expense_service.list_transactions,
        )
    except SQLAlchemyError as error:
        LOGGER.warning(
            "Transactions fetch for AI analysis failed",
            extra={"error_type": type(error).__name__},
        )
        return web.json_response(
            data={"detail": DATABASE_ERROR_MESSAGE},
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
            headers=CORS_HEADERS,
        )

    analysis_service = request.app[ANALYSIS_SERVICE_KEY]
    try:
        analysis: TransactionAnalysisResult = await asyncio.to_thread(
            analysis_service.analyze_transactions,
            transactions=transactions,
        )
    except TransactionLimitExceededError as error:
        LOGGER.warning(
            "AI analysis skipped due to transaction limit",
            extra={"transaction_count": len(transactions)},
        )
        return web.json_response(
            data={"detail": str(error)},
            status=HTTPStatus.BAD_REQUEST,
            headers=CORS_HEADERS,
        )
    except GeminiConfigurationError as error:
        LOGGER.warning(
            "Gemini configuration invalid",
            extra={"error_type": type(error).__name__},
        )
        return web.json_response(
            data={"detail": AI_CONFIG_ERROR_MESSAGE},
            status=HTTPStatus.SERVICE_UNAVAILABLE,
            headers=CORS_HEADERS,
        )
    except GeminiAnalysisError as error:
        LOGGER.warning(
            "Gemini analysis failed across all fallback models",
            extra={"error_type": type(error).__name__},
        )
        return web.json_response(
            data={"detail": str(error)},
            status=HTTPStatus.SERVICE_UNAVAILABLE,
            headers=CORS_HEADERS,
        )
    except PromptTemplateError as error:
        LOGGER.error(
            "AI prompt template loading failed",
            extra={"error_type": type(error).__name__},
        )
        return web.json_response(
            data={"detail": "Помилка конфігурації AI-промпта на сервері."},
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
            headers=CORS_HEADERS,
        )

    return web.json_response(
        data=_serialize_transaction_analysis(analysis),
        status=HTTPStatus.OK,
        headers=CORS_HEADERS,
    )


def _serialize_transaction_analysis(
    analysis: TransactionAnalysisResult,
) -> dict[str, str | list[str]]:
    return {
        "summary": analysis.summary,
        "top_expense_categories": analysis.top_expense_categories,
        "risks": analysis.risks,
        "advice": analysis.advice,
    }


def resolve_api_address() -> tuple[str, int]:
    host = os.getenv(API_HOST_VARIABLE, DEFAULT_API_HOST).strip() or DEFAULT_API_HOST
    raw_port = os.getenv(API_PORT_VARIABLE, str(DEFAULT_API_PORT)).strip()
    return host, int(raw_port)
