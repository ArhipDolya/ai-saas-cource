import asyncio
import logging
import os
from http import HTTPStatus

from aiohttp import web
from sqlalchemy.exc import SQLAlchemyError

from app.expenses import ExpenseService, FinancialSummary, TransactionRecord


TRANSACTIONS_PATH = "/api/transactions"
SUMMARY_PATH = "/api/summary"
DEFAULT_API_HOST = "0.0.0.0"  # Bind all interfaces for local and Docker access
DEFAULT_API_PORT = 8000  # Default HTTP port for backend API
API_HOST_VARIABLE = "API_HOST"
API_PORT_VARIABLE = "API_PORT"
CORS_HEADERS = {"Access-Control-Allow-Origin": "*"}
DATABASE_ERROR_MESSAGE = "Failed to fetch transactions."
SUMMARY_ERROR_MESSAGE = "Failed to fetch summary."
EXPENSE_SERVICE_KEY: web.AppKey[ExpenseService] = web.AppKey(
    "expense_service",
    ExpenseService,
)
LOGGER = logging.getLogger(__name__)


async def start_api_server(
    *,
    expense_service: ExpenseService,
    host: str = DEFAULT_API_HOST,
    port: int = DEFAULT_API_PORT,
) -> web.AppRunner:
    app = create_api_app(expense_service=expense_service)
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


def create_api_app(expense_service: ExpenseService) -> web.Application:
    app = web.Application()
    app[EXPENSE_SERVICE_KEY] = expense_service
    app.router.add_get(TRANSACTIONS_PATH, handle_get_transactions)
    app.router.add_get(SUMMARY_PATH, handle_get_summary)
    return app


async def handle_get_transactions(request: web.Request) -> web.Response:
    expense_service = request.app[EXPENSE_SERVICE_KEY]
    try:
        transactions: list[TransactionRecord] = await asyncio.to_thread(
            expense_service.list_transactions,
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


def resolve_api_address() -> tuple[str, int]:
    host = os.getenv(API_HOST_VARIABLE, DEFAULT_API_HOST).strip() or DEFAULT_API_HOST
    raw_port = os.getenv(API_PORT_VARIABLE, str(DEFAULT_API_PORT)).strip()
    return host, int(raw_port)
