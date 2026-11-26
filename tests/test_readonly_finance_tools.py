import unittest
from datetime import datetime
from decimal import Decimal

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from app.expenses import (
    DEFAULT_TOP_EXPENSES_LIMIT,
    MAX_TOP_EXPENSES_LIMIT,
    MIN_TOP_EXPENSES_LIMIT,
    ExpenseService,
    TransactionValidationError,
    parse_analytics_period,
    validate_top_expenses_limit,
)
from app.tools import (
    build_readonly_finance_tools,
    langchain_tools_to_gemini_declarations,
)


def create_seeded_sqlite_engine() -> Engine:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        execution_options={"schema_translate_map": {"public": None}},
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL UNIQUE,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """,
            ),
        )
        connection.execute(
            text(
                """
                CREATE TABLE categories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    name VARCHAR(100) NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (user_id, name)
                )
                """,
            ),
        )
        connection.execute(
            text(
                """
                CREATE TABLE transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    category_id INTEGER NOT NULL,
                    amount NUMERIC(12, 2) NOT NULL,
                    transaction_type VARCHAR(10) NOT NULL DEFAULT 'expense',
                    description VARCHAR(255),
                    created_at DATETIME NOT NULL
                )
                """,
            ),
        )
        connection.execute(
            text(
                """
                CREATE TABLE incomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    category_id INTEGER NOT NULL,
                    amount NUMERIC(12, 2) NOT NULL,
                    description VARCHAR(255),
                    created_at DATETIME NOT NULL
                )
                """,
            ),
        )
        connection.execute(
            text("INSERT INTO users (id, telegram_id) VALUES (1, 1001)"),
        )
        connection.execute(
            text(
                """
                INSERT INTO categories (id, user_id, name) VALUES
                    (1, 1, 'зарплата'),
                    (2, 1, 'кава'),
                    (3, 1, 'паливо'),
                    (4, 1, 'бонус')
                """,
            ),
        )
        connection.execute(
            text(
                """
                INSERT INTO incomes (id, user_id, category_id, amount, description, created_at) VALUES
                    (1, 1, 1, 15000.00, 'Листопадова зарплата', '2026-11-01 09:00:00'),
                    (2, 1, 1, 14000.00, 'Жовтнева зарплата', '2026-10-15 09:00:00')
                """,
            ),
        )
        connection.execute(
            text(
                """
                INSERT INTO transactions (id, user_id, category_id, amount, transaction_type, description, created_at) VALUES
                    (1, 1, 4, 2000.00, 'income', 'Премія', '2026-11-20 14:00:00'),
                    (2, 1, 2, 120.00, 'expense', 'Лате', '2026-11-25 08:30:00'),
                    (3, 1, 2, 80.00, 'expense', 'Еспресо', '2026-11-26 09:00:00'),
                    (4, 1, 3, 950.00, 'expense', 'АЗС', '2026-11-24 18:00:00'),
                    (5, 1, 3, 500.00, 'expense', 'Стара заправка', '2026-10-10 12:00:00')
                """,
            ),
        )
    return engine


class PeriodValidationTests(unittest.TestCase):
    def test_parse_analytics_period_supports_named_month_and_custom_ranges(
        self,
    ) -> None:
        reference_now = datetime(2026, 11, 26, 15, 30, 0)

        all_range = parse_analytics_period(period=None, reference_now=reference_now)
        self.assertEqual(all_range.period, "all")
        self.assertIsNone(all_range.start_at)
        self.assertIsNone(all_range.end_at)

        self.assertEqual(
            parse_analytics_period(period="  ", reference_now=reference_now).period,
            "all",
        )

        today_range = parse_analytics_period(
            period="today", reference_now=reference_now
        )
        self.assertEqual(today_range.start_at, datetime(2026, 11, 26, 0, 0, 0))
        self.assertEqual(today_range.end_at, datetime(2026, 11, 26, 23, 59, 59))

        week_range = parse_analytics_period(period="week", reference_now=reference_now)
        self.assertEqual(week_range.start_at, datetime(2026, 11, 20, 0, 0, 0))
        seven_day_range = parse_analytics_period(
            period="7d",
            reference_now=reference_now,
        )
        self.assertEqual(seven_day_range.start_at, week_range.start_at)

        month_range = parse_analytics_period(
            period="month", reference_now=reference_now
        )
        self.assertEqual(month_range.start_at, datetime(2026, 11, 1, 0, 0, 0))
        self.assertEqual(month_range.end_at, datetime(2026, 11, 26, 23, 59, 59))

        thirty_day_range = parse_analytics_period(
            period="30d",
            reference_now=reference_now,
        )
        self.assertEqual(thirty_day_range.start_at, datetime(2026, 10, 28, 0, 0, 0))

        year_range = parse_analytics_period(period="year", reference_now=reference_now)
        self.assertEqual(year_range.start_at, datetime(2026, 1, 1, 0, 0, 0))

        explicit_month = parse_analytics_period(
            period="2026-02",
            reference_now=reference_now,
        )
        self.assertEqual(explicit_month.start_at, datetime(2026, 2, 1, 0, 0, 0))
        self.assertEqual(explicit_month.end_at, datetime(2026, 2, 28, 23, 59, 59))

        custom_range = parse_analytics_period(
            period="2026-11-10:2026-11-25",
            reference_now=reference_now,
        )
        self.assertEqual(custom_range.start_at, datetime(2026, 11, 10, 0, 0, 0))
        self.assertEqual(custom_range.end_at, datetime(2026, 11, 25, 23, 59, 59))

    def test_rejects_invalid_periods_and_invalid_limits(self) -> None:
        invalid_periods: list[object] = [
            123,
            "last_decade",
            "2026-13",
            "1999-05",
            "2026-11-31:2026-12-01",
            "2026-11-25:2026-11-10",
            "1999-01-01:2026-11-01",
        ]
        for period in invalid_periods:
            with self.subTest(period=period):
                with self.assertRaises(TransactionValidationError):
                    parse_analytics_period(period=period)  # type: ignore[arg-type]

        invalid_limits: list[object] = [
            True,
            False,
            "5",
            MIN_TOP_EXPENSES_LIMIT - 1,
            MAX_TOP_EXPENSES_LIMIT + 1,
        ]
        for limit in invalid_limits:
            with self.subTest(limit=limit):
                with self.assertRaises(TransactionValidationError):
                    validate_top_expenses_limit(limit)

        self.assertEqual(
            validate_top_expenses_limit(MIN_TOP_EXPENSES_LIMIT),
            MIN_TOP_EXPENSES_LIMIT,
        )
        self.assertEqual(
            validate_top_expenses_limit(MAX_TOP_EXPENSES_LIMIT),
            MAX_TOP_EXPENSES_LIMIT,
        )


class ReadOnlyToolsSqlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_seeded_sqlite_engine()
        self.expense_service = ExpenseService(self.engine)
        self.tools = build_readonly_finance_tools(
            expense_service=self.expense_service,
        )
        self.tools_by_name = {item.name: item for item in self.tools}

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_only_three_readonly_tools_registered_and_db_remains_unmodified(
        self,
    ) -> None:
        self.assertEqual(
            set(self.tools_by_name.keys()),
            {
                "get_transactions_summary",
                "get_category_totals",
                "get_top_expenses",
            },
        )
        declarations = langchain_tools_to_gemini_declarations(self.tools)
        self.assertEqual(len(declarations), 3)
        for decl in declarations:
            params = decl["parameters"]
            self.assertIsInstance(params, dict)
            assert isinstance(params, dict)
            self.assertNotIn("title", params)

        before_ops = self.expense_service.list_transactions()
        self.tools_by_name["get_transactions_summary"].invoke({"period": "all"})
        self.tools_by_name["get_category_totals"].invoke({"period": "2026-11"})
        self.tools_by_name["get_top_expenses"].invoke(
            {"period": "all", "limit": DEFAULT_TOP_EXPENSES_LIMIT},
        )
        after_ops = self.expense_service.list_transactions()
        self.assertEqual(before_ops, after_ops)

    def test_get_transactions_summary_returns_exact_totals_and_empty_period_zeros(
        self,
    ) -> None:
        november = self.expense_service.get_transactions_summary_for_period(
            period="2026-11",
        )
        self.assertEqual(november.total_income, Decimal("17000.00"))
        self.assertEqual(november.total_expense, Decimal("1150.00"))
        self.assertEqual(november.balance, Decimal("15850.00"))
        self.assertEqual(november.income_count, 2)
        self.assertEqual(november.expense_count, 3)
        self.assertEqual(november.total_count, 5)

        empty_month = self.expense_service.get_transactions_summary_for_period(
            period="2025-01",
        )
        self.assertEqual(empty_month.total_income, Decimal("0.00"))
        self.assertEqual(empty_month.total_expense, Decimal("0.00"))
        self.assertEqual(empty_month.balance, Decimal("0.00"))
        self.assertEqual(empty_month.total_count, 0)

        tool_output = self.tools_by_name["get_transactions_summary"].invoke(
            {"period": "2026-11"},
        )
        self.assertEqual(tool_output["total_income"], 17000.0)
        self.assertEqual(tool_output["total_expense"], 1150.0)
        self.assertEqual(tool_output["currency"], "UAH")

    def test_get_category_totals_groups_and_sorts_separately_by_type(self) -> None:
        result = self.tools_by_name["get_category_totals"].invoke(
            {"period": "2026-11"},
        )
        self.assertEqual(
            result["expense_categories"],
            [
                {"category": "паливо", "total_amount": 950.0, "transactions_count": 1},
                {"category": "кава", "total_amount": 200.0, "transactions_count": 2},
            ],
        )
        self.assertEqual(
            result["income_categories"],
            [
                {
                    "category": "зарплата",
                    "total_amount": 15000.0,
                    "transactions_count": 1,
                },
                {
                    "category": "бонус",
                    "total_amount": 2000.0,
                    "transactions_count": 1,
                },
            ],
        )

    def test_get_top_expenses_excludes_incomes_and_honors_limit(self) -> None:
        result = self.tools_by_name["get_top_expenses"].invoke(
            {"period": "2026-11", "limit": 2},
        )
        expenses = result["expenses"]
        self.assertEqual(len(expenses), 2)
        self.assertEqual(expenses[0]["category_name"], "паливо")
        self.assertEqual(expenses[0]["amount"], 950.0)
        self.assertEqual(expenses[1]["category_name"], "кава")
        self.assertEqual(expenses[1]["amount"], 120.0)
