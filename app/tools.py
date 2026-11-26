from datetime import datetime

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field

from app.expenses import (
    DEFAULT_TOP_EXPENSES_LIMIT,
    MAX_TOP_EXPENSES_LIMIT,
    MIN_TOP_EXPENSES_LIMIT,
    PERIOD_ALL,
    ExpenseService,
)
from app.models import EXPENSE_TRANSACTION_TYPE, INCOME_TRANSACTION_TYPE


FINANCE_CURRENCY = "UAH"
PERIOD_FIELD_DESCRIPTION = (
    "Time period filter: 'all', 'today', 'week', '7d', 'month', '30d', 'year', "
    "'YYYY-MM' (e.g. '2026-11'), or 'YYYY-MM-DD:YYYY-MM-DD'."
)


class PeriodToolArgs(BaseModel):
    period: str = Field(
        default=PERIOD_ALL,
        description=PERIOD_FIELD_DESCRIPTION,
    )


class TopExpensesToolArgs(BaseModel):
    period: str = Field(
        default=PERIOD_ALL,
        description=PERIOD_FIELD_DESCRIPTION,
    )
    limit: int = Field(
        default=DEFAULT_TOP_EXPENSES_LIMIT,
        ge=MIN_TOP_EXPENSES_LIMIT,
        le=MAX_TOP_EXPENSES_LIMIT,
        description=(
            f"Maximum number of top expense records to return "
            f"({MIN_TOP_EXPENSES_LIMIT}..{MAX_TOP_EXPENSES_LIMIT})."
        ),
    )


def build_readonly_finance_tools(
    *,
    expense_service: ExpenseService,
) -> list[BaseTool]:
    """Build strictly read-only LangChain financial tools backed by PostgreSQL."""

    @tool("get_transactions_summary", args_schema=PeriodToolArgs)
    def get_transactions_summary(period: str = PERIOD_ALL) -> dict[str, object]:
        """Get read-only total income, total expenses, balance, and counts for a period."""
        summary = expense_service.get_transactions_summary_for_period(period=period)
        return {
            "period": summary.period,
            "start_date": _format_optional_datetime(summary.start_at),
            "end_date": _format_optional_datetime(summary.end_at),
            "total_income": float(summary.total_income),
            "total_expense": float(summary.total_expense),
            "balance": float(summary.balance),
            "income_count": summary.income_count,
            "expense_count": summary.expense_count,
            "total_count": summary.total_count,
            "currency": FINANCE_CURRENCY,
        }

    @tool("get_category_totals", args_schema=PeriodToolArgs)
    def get_category_totals(period: str = PERIOD_ALL) -> dict[str, object]:
        """Get read-only spending and income totals grouped by category for a period."""
        category_rows = expense_service.get_category_totals_for_period(period=period)
        expense_categories = [
            {
                "category": row.category_name,
                "total_amount": float(row.total_amount),
                "transactions_count": row.transactions_count,
            }
            for row in category_rows
            if row.transaction_type == EXPENSE_TRANSACTION_TYPE
        ]
        income_categories = [
            {
                "category": row.category_name,
                "total_amount": float(row.total_amount),
                "transactions_count": row.transactions_count,
            }
            for row in category_rows
            if row.transaction_type == INCOME_TRANSACTION_TYPE
        ]
        return {
            "period": period.strip().lower() if period else PERIOD_ALL,
            "expense_categories": expense_categories,
            "income_categories": income_categories,
            "currency": FINANCE_CURRENCY,
        }

    @tool("get_top_expenses", args_schema=TopExpensesToolArgs)
    def get_top_expenses(
        period: str = PERIOD_ALL,
        limit: int = DEFAULT_TOP_EXPENSES_LIMIT,
    ) -> dict[str, object]:
        """Get read-only largest individual expense operations for a period."""
        top_expenses = expense_service.get_top_expenses_for_period(
            period=period,
            limit=limit,
        )
        return {
            "period": period.strip().lower() if period else PERIOD_ALL,
            "limit": limit,
            "expenses": [
                {
                    "id": item.id,
                    "amount": float(item.amount),
                    "category_name": item.category_name,
                    "description": item.description,
                    "created_at": item.created_at.isoformat(),
                }
                for item in top_expenses
            ],
            "currency": FINANCE_CURRENCY,
        }

    return [
        get_transactions_summary,
        get_category_totals,
        get_top_expenses,
    ]


def langchain_tools_to_gemini_declarations(
    tools: list[BaseTool],
) -> list[dict[str, object]]:
    """Convert LangChain BaseTool objects into Gemini REST functionDeclarations."""
    declarations: list[dict[str, object]] = []
    for current_tool in tools:
        raw_schema = current_tool.get_input_schema().model_json_schema()
        parameters = _sanitize_gemini_schema_node(raw_schema)
        declarations.append(
            {
                "name": current_tool.name,
                "description": current_tool.description,
                "parameters": parameters,
            },
        )
    return declarations


def _sanitize_gemini_schema_node(node: object) -> dict[str, object]:
    if not isinstance(node, dict):
        return {}

    cleaned: dict[str, object] = {}
    schema_type = node.get("type")
    if isinstance(schema_type, str):
        cleaned["type"] = schema_type

    description = node.get("description")
    if isinstance(description, str) and description:
        cleaned["description"] = description

    enum_values = node.get("enum")
    if isinstance(enum_values, list):
        cleaned["enum"] = enum_values

    properties = node.get("properties")
    if isinstance(properties, dict):
        cleaned["properties"] = {
            str(key): _sanitize_gemini_schema_node(value)
            for key, value in properties.items()
        }

    required = node.get("required")
    if isinstance(required, list):
        cleaned["required"] = required

    return cleaned


def _format_optional_datetime(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
