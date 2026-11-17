import re
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, literal, select, union_all
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection, Engine

from app.models import (
    CATEGORIES,
    CATEGORY_NAME_MAX_LENGTH,
    DESCRIPTION_MAX_LENGTH,
    EXPENSE_TRANSACTION_TYPE,
    INCOME_TRANSACTION_TYPE,
    INCOMES,
    TRANSACTIONS,
    USERS,
)


EXPENSE_USAGE = "Використай команду: /expense 120 кава"
AMOUNT_PATTERN = re.compile(r"[0-9]+(?:[.,][0-9]{1,2})?")
DATE_ONLY_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
ZERO_AMOUNT = Decimal("0.00")
MINIMUM_EXPENSE_AMOUNT = Decimal("0.01")
MAXIMUM_EXPENSE_AMOUNT = Decimal("9999999999.99")  # NUMERIC(12, 2).
MINIMUM_TRANSACTION_AMOUNT = Decimal("0.01")
MAXIMUM_TRANSACTION_AMOUNT = Decimal("10000000.00")  # Cap well below 1 billion.
AMOUNT_QUANTUM = Decimal("0.01")
MAX_AMOUNT_DECIMAL_PLACES = 2
MIN_TRANSACTION_ID = 1
MAX_BIGINT_ID = 9_223_372_036_854_775_807  # PostgreSQL signed BIGINT max.
MIN_TRANSACTION_YEAR = 2000
MAX_TRANSACTION_YEAR = 2100
DEFAULT_WEB_TELEGRAM_ID = 0  # Fallback owner when no Telegram user exists yet.
ALL_TRANSACTIONS_FILTER = "all"
ALLOWED_TRANSACTION_TYPES = frozenset(
    {
        INCOME_TRANSACTION_TYPE,
        EXPENSE_TRANSACTION_TYPE,
    },
)
EXPENSE_ARGUMENT_COUNT = 2
CATEGORY_SPLIT_LIMIT = 1  # The category may contain multiple words.


class ExpenseValidationError(ValueError):
    """The expense cannot be stored without changing the user's input."""


class TransactionValidationError(ValueError):
    """The API transaction payload failed domain validation."""


class TransactionNotFoundError(LookupError):
    """No matching transaction or income record exists for the given id."""


class TransactionConflictError(RuntimeError):
    """Both transactions and incomes contain a row with the same id."""


@dataclass(frozen=True, kw_only=True)
class ExpenseInput:
    amount: Decimal
    category_name: str


@dataclass(frozen=True, kw_only=True)
class CreatedExpense:
    transaction_id: int
    amount: Decimal
    category_name: str


@dataclass(frozen=True, kw_only=True)
class TransactionRecord:
    id: int
    user_id: int
    telegram_id: int
    category_id: int
    category_name: str
    transaction_type: str
    amount: Decimal
    description: str | None
    created_at: datetime


@dataclass(frozen=True, kw_only=True)
class FinancialSummary:
    total_income: Decimal
    total_expense: Decimal
    balance: Decimal


@dataclass(frozen=True, kw_only=True)
class CreateTransactionInput:
    transaction_type: str
    amount: Decimal
    category_name: str
    created_at: datetime
    description: str | None = None
    telegram_id: int | None = None


@dataclass(frozen=True, kw_only=True)
class DeletedTransaction:
    id: int
    transaction_type: str


def parse_transaction_id(raw_id: object) -> int:
    """Validate that a transaction identifier is a positive 64-bit integer."""
    if isinstance(raw_id, bool):
        raise TransactionValidationError(
            "ID транзакції має бути додатним цілим числом.",
        )
    if isinstance(raw_id, int):
        transaction_id = raw_id
    elif isinstance(raw_id, str):
        id_text = raw_id.strip()
        if not id_text or not id_text.isdigit():
            raise TransactionValidationError(
                "ID транзакції має бути додатним цілим числом.",
            )
        transaction_id = int(id_text)
    else:
        raise TransactionValidationError(
            "ID транзакції має бути додатним цілим числом.",
        )

    is_in_bigint_range = MIN_TRANSACTION_ID <= transaction_id <= MAX_BIGINT_ID
    if not is_in_bigint_range:
        raise TransactionValidationError(
            f"ID транзакції має бути від {MIN_TRANSACTION_ID} до {MAX_BIGINT_ID}.",
        )
    return transaction_id


def parse_optional_transaction_type(raw_type: object) -> str | None:
    """Validate an optional transaction type filter ('income' or 'expense')."""
    if raw_type is None:
        return None
    if not isinstance(raw_type, str):
        raise TransactionValidationError(
            "Параметр type має бути 'income' або 'expense'.",
        )
    normalized_type = raw_type.strip()
    if not normalized_type:
        return None
    if normalized_type not in ALLOWED_TRANSACTION_TYPES:
        raise TransactionValidationError(
            "Неприпустимий тип операції. "
            "Дозволено лише: income (дохід) або expense (витрата).",
        )
    return normalized_type


def parse_transaction_filter(raw_type: object) -> str | None:
    """Validate GET /api/transactions query filter ('all', 'income', or 'expense')."""
    if raw_type is None:
        return None
    if not isinstance(raw_type, str):
        raise TransactionValidationError(
            "Параметр type має бути 'all', 'income' або 'expense'.",
        )
    normalized_type = raw_type.strip().lower()
    if not normalized_type or normalized_type == ALL_TRANSACTIONS_FILTER:
        return None
    if normalized_type not in ALLOWED_TRANSACTION_TYPES:
        raise TransactionValidationError(
            "Неприпустимий фільтр type. "
            "Дозволено лише: all, income (дохід) або expense (витрата).",
        )
    return normalized_type


def parse_transaction_payload(payload: object) -> CreateTransactionInput:
    """Validate and normalize a JSON transaction payload from the web API."""
    if not isinstance(payload, dict):
        raise TransactionValidationError("Очікується JSON-об'єкт у тілі запиту.")

    transaction_type = _parse_transaction_type(payload.get("type"))
    amount = _parse_transaction_amount(payload.get("amount"))
    category_name = _parse_transaction_category(payload.get("category"))
    created_at = _parse_transaction_date(payload.get("date"))
    description = _parse_transaction_description(payload.get("description"))

    raw_telegram_id = payload.get("telegram_id")
    telegram_id: int | None = None
    if raw_telegram_id is not None:
        is_valid_telegram_id = (
            isinstance(raw_telegram_id, int)
            and not isinstance(raw_telegram_id, bool)
            and raw_telegram_id > 0
        )
        if not is_valid_telegram_id:
            raise TransactionValidationError(
                "Поле telegram_id має бути додатним цілим числом.",
            )
        telegram_id = raw_telegram_id

    return CreateTransactionInput(
        transaction_type=transaction_type,
        amount=amount,
        category_name=category_name,
        created_at=created_at,
        description=description,
        telegram_id=telegram_id,
    )


def _parse_transaction_type(raw_type: object) -> str:
    if not isinstance(raw_type, str):
        raise TransactionValidationError(
            "Поле type є обов'язковим: оберіть income або expense.",
        )

    normalized_type = raw_type.strip()
    if normalized_type not in ALLOWED_TRANSACTION_TYPES:
        raise TransactionValidationError(
            "Неприпустимий тип операції. "
            "Дозволено лише: income (дохід) або expense (витрата).",
        )
    return normalized_type


def _parse_transaction_amount(raw_amount: object) -> Decimal:
    if raw_amount is None or isinstance(raw_amount, bool):
        raise TransactionValidationError("Сума має бути додатним числом.")

    if isinstance(raw_amount, int):
        amount_text = str(raw_amount)
    elif isinstance(raw_amount, float):
        try:
            parsed_decimal = Decimal(str(raw_amount))
        except InvalidOperation:
            raise TransactionValidationError(
                "Сума має бути коректним числом.",
            ) from None
        if not parsed_decimal.is_finite():
            raise TransactionValidationError("Сума має бути скінченним числом.")
        amount_text = format(parsed_decimal, "f")
    elif isinstance(raw_amount, str):
        amount_text = raw_amount.strip().replace(",", ".")
    else:
        raise TransactionValidationError("Сума має бути додатним числом.")

    if not amount_text:
        raise TransactionValidationError("Сума є обов'язковим полем.")

    try:
        amount = Decimal(amount_text)
    except InvalidOperation:
        raise TransactionValidationError("Сума має бути коректним числом.") from None

    if not amount.is_finite():
        raise TransactionValidationError("Сума має бути скінченним числом.")
    if amount <= ZERO_AMOUNT:
        raise TransactionValidationError("Сума має бути більшою за нуль.")
    if amount < MINIMUM_TRANSACTION_AMOUNT:
        raise TransactionValidationError(
            f"Мінімальна сума операції: {MINIMUM_TRANSACTION_AMOUNT} грн.",
        )
    if amount > MAXIMUM_TRANSACTION_AMOUNT:
        raise TransactionValidationError(
            f"Максимальна сума операції: {MAXIMUM_TRANSACTION_AMOUNT} грн.",
        )

    exponent = amount.as_tuple().exponent
    has_too_many_decimals = (
        isinstance(exponent, int) and exponent < -MAX_AMOUNT_DECIMAL_PLACES
    )
    if has_too_many_decimals:
        raise TransactionValidationError(
            "Сума може містити максимум два знаки після коми.",
        )

    return amount.quantize(AMOUNT_QUANTUM)


def _parse_transaction_category(raw_category: object) -> str:
    if not isinstance(raw_category, str):
        raise TransactionValidationError("Категорія є обов'язковим текстовим полем.")

    category_name = " ".join(raw_category.split())
    if not category_name:
        raise TransactionValidationError("Категорія не може бути порожньою.")
    if len(category_name) > CATEGORY_NAME_MAX_LENGTH:
        raise TransactionValidationError(
            "Назва категорії має містити не більше "
            f"{CATEGORY_NAME_MAX_LENGTH} символів.",
        )
    return category_name


def _parse_transaction_date(raw_date: object) -> datetime:
    current_timestamp = datetime.now().replace(microsecond=0)
    if raw_date is None:
        return current_timestamp
    if not isinstance(raw_date, str):
        raise TransactionValidationError(
            "Дата має бути рядком у форматі YYYY-MM-DD.",
        )

    date_text = raw_date.strip()
    if not date_text:
        return current_timestamp

    if DATE_ONLY_PATTERN.fullmatch(date_text):
        try:
            parsed_date = date.fromisoformat(date_text)
        except ValueError:
            raise TransactionValidationError(
                "Некоректна дата. Використовуйте формат YYYY-MM-DD.",
            ) from None
        is_year_in_range = (
            MIN_TRANSACTION_YEAR <= parsed_date.year <= MAX_TRANSACTION_YEAR
        )
        if not is_year_in_range:
            raise TransactionValidationError(
                "Рік дати має бути в межах "
                f"{MIN_TRANSACTION_YEAR}-{MAX_TRANSACTION_YEAR}.",
            )
        current_time: time = current_timestamp.time()
        return datetime.combine(parsed_date, current_time)

    try:
        parsed_datetime = datetime.fromisoformat(date_text.replace("Z", "+00:00"))
    except ValueError:
        raise TransactionValidationError(
            "Некоректний формат дати. Використовуйте YYYY-MM-DD.",
        ) from None

    is_year_in_range = (
        MIN_TRANSACTION_YEAR <= parsed_datetime.year <= MAX_TRANSACTION_YEAR
    )
    if not is_year_in_range:
        raise TransactionValidationError(
            f"Рік дати має бути в межах {MIN_TRANSACTION_YEAR}-{MAX_TRANSACTION_YEAR}.",
        )
    if parsed_datetime.tzinfo is not None:
        return parsed_datetime.astimezone().replace(tzinfo=None, microsecond=0)
    return parsed_datetime.replace(microsecond=0)


def _parse_transaction_description(raw_description: object) -> str | None:
    if raw_description is None:
        return None
    if not isinstance(raw_description, str):
        raise TransactionValidationError("Опис має бути текстовим рядком.")

    description = " ".join(raw_description.split())
    if not description:
        return None
    if len(description) > DESCRIPTION_MAX_LENGTH:
        raise TransactionValidationError(
            f"Опис має містити не більше {DESCRIPTION_MAX_LENGTH} символів.",
        )
    return description


def parse_expense(command_args: str | None) -> ExpenseInput:
    if not command_args:
        raise ExpenseValidationError(EXPENSE_USAGE)

    arguments = command_args.split(maxsplit=CATEGORY_SPLIT_LIMIT)
    has_required_arguments = len(arguments) == EXPENSE_ARGUMENT_COUNT
    if not has_required_arguments:
        raise ExpenseValidationError(EXPENSE_USAGE)

    amount_text, category_text = arguments
    if not AMOUNT_PATTERN.fullmatch(amount_text):
        raise ExpenseValidationError(
            "Сума має бути додатним числом із максимум двома знаками після коми.",
        )

    amount = Decimal(amount_text.replace(",", "."))
    if amount < MINIMUM_EXPENSE_AMOUNT:
        raise ExpenseValidationError("Сума має бути більшою за нуль.")
    if amount > MAXIMUM_EXPENSE_AMOUNT:
        raise ExpenseValidationError("Максимальна сума витрати: 9999999999.99 грн.")

    category_name = " ".join(category_text.split())
    if len(category_name) > CATEGORY_NAME_MAX_LENGTH:
        raise ExpenseValidationError(
            f"Назва категорії має містити не більше {CATEGORY_NAME_MAX_LENGTH} символів.",
        )

    return ExpenseInput(
        amount=amount.quantize(AMOUNT_QUANTUM),
        category_name=category_name,
    )


class ExpenseService:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create_expense(
        self,
        *,
        telegram_id: int,
        expense: ExpenseInput,
    ) -> CreatedExpense:
        with self._engine.begin() as connection:
            user_id = self._get_or_create_user_id(
                connection=connection,
                telegram_id=telegram_id,
            )
            category_id = self._get_or_create_category_id(
                connection=connection,
                user_id=user_id,
                category_name=expense.category_name,
            )
            transaction_id: int = connection.execute(
                TRANSACTIONS.insert()
                .values(
                    user_id=user_id,
                    category_id=category_id,
                    amount=expense.amount,
                )
                .returning(TRANSACTIONS.c.id),
            ).scalar_one()

        return CreatedExpense(
            transaction_id=transaction_id,
            amount=expense.amount,
            category_name=expense.category_name,
        )

    def _get_or_create_user_id(
        self,
        *,
        connection: Connection,
        telegram_id: int,
    ) -> int:
        connection.execute(
            insert(USERS)
            .values(telegram_id=telegram_id)
            .on_conflict_do_nothing(index_elements=[USERS.c.telegram_id]),
        )
        user_id: int = connection.execute(
            select(USERS.c.id).where(USERS.c.telegram_id == telegram_id),
        ).scalar_one()
        return user_id

    def _get_or_create_category_id(
        self,
        *,
        connection: Connection,
        user_id: int,
        category_name: str,
    ) -> int:
        connection.execute(
            insert(CATEGORIES)
            .values(user_id=user_id, name=category_name)
            .on_conflict_do_nothing(
                index_elements=[CATEGORIES.c.user_id, CATEGORIES.c.name],
            ),
        )
        category_id: int = connection.execute(
            select(CATEGORIES.c.id).where(
                CATEGORIES.c.user_id == user_id,
                CATEGORIES.c.name == category_name,
            ),
        ).scalar_one()
        return category_id

    def list_transactions(
        self,
        *,
        transaction_type: str | None = None,
    ) -> list[TransactionRecord]:
        transactions_query = select(
            TRANSACTIONS.c.id,
            TRANSACTIONS.c.user_id,
            USERS.c.telegram_id,
            TRANSACTIONS.c.category_id,
            CATEGORIES.c.name.label("category_name"),
            TRANSACTIONS.c.transaction_type,
            TRANSACTIONS.c.amount,
            TRANSACTIONS.c.description,
            TRANSACTIONS.c.created_at,
        ).select_from(
            TRANSACTIONS.join(
                CATEGORIES,
                (TRANSACTIONS.c.category_id == CATEGORIES.c.id)
                & (TRANSACTIONS.c.user_id == CATEGORIES.c.user_id),
            ).join(
                USERS,
                TRANSACTIONS.c.user_id == USERS.c.id,
            ),
        )
        incomes_query = select(
            INCOMES.c.id,
            INCOMES.c.user_id,
            USERS.c.telegram_id,
            INCOMES.c.category_id,
            CATEGORIES.c.name.label("category_name"),
            literal(INCOME_TRANSACTION_TYPE).label("transaction_type"),
            INCOMES.c.amount,
            INCOMES.c.description,
            INCOMES.c.created_at,
        ).select_from(
            INCOMES.join(
                CATEGORIES,
                (INCOMES.c.category_id == CATEGORIES.c.id)
                & (INCOMES.c.user_id == CATEGORIES.c.user_id),
            ).join(
                USERS,
                INCOMES.c.user_id == USERS.c.id,
            ),
        )
        combined_operations = union_all(
            transactions_query,
            incomes_query,
        ).subquery()
        statement = select(combined_operations)
        if transaction_type is not None:
            statement = statement.where(
                combined_operations.c.transaction_type == transaction_type,
            )
        statement = statement.order_by(
            combined_operations.c.created_at.desc(),
            combined_operations.c.id.desc(),
        )
        with self._engine.connect() as connection:
            rows = connection.execute(statement).all()

        return [
            TransactionRecord(
                id=row.id,
                user_id=row.user_id,
                telegram_id=row.telegram_id,
                category_id=row.category_id,
                category_name=row.category_name,
                transaction_type=row.transaction_type,
                amount=row.amount,
                description=row.description,
                created_at=row.created_at,
            )
            for row in rows
        ]

    def get_summary(self) -> FinancialSummary:
        incomes_table_sum = select(
            func.coalesce(func.sum(INCOMES.c.amount), ZERO_AMOUNT),
        ).scalar_subquery()
        transactions_income_sum = (
            select(
                func.coalesce(func.sum(TRANSACTIONS.c.amount), ZERO_AMOUNT),
            )
            .where(TRANSACTIONS.c.transaction_type == INCOME_TRANSACTION_TYPE)
            .scalar_subquery()
        )
        expense_subquery = (
            select(
                func.coalesce(func.sum(TRANSACTIONS.c.amount), ZERO_AMOUNT),
            )
            .where(TRANSACTIONS.c.transaction_type == EXPENSE_TRANSACTION_TYPE)
            .scalar_subquery()
        )
        statement = select(
            (incomes_table_sum + transactions_income_sum).label("total_income"),
            expense_subquery.label("total_expense"),
        )
        with self._engine.connect() as connection:
            row = connection.execute(statement).one()

        total_income = Decimal(row.total_income).quantize(AMOUNT_QUANTUM)
        total_expense = Decimal(row.total_expense).quantize(AMOUNT_QUANTUM)
        balance = (total_income - total_expense).quantize(AMOUNT_QUANTUM)
        return FinancialSummary(
            total_income=total_income,
            total_expense=total_expense,
            balance=balance,
        )

    def create_transaction(
        self,
        *,
        transaction: CreateTransactionInput,
    ) -> TransactionRecord:
        """Persist a validated income or expense operation in PostgreSQL."""
        with self._engine.begin() as connection:
            user_id, telegram_id = self._resolve_user(
                connection=connection,
                telegram_id=transaction.telegram_id,
            )
            category_id = self._get_or_create_category_id(
                connection=connection,
                user_id=user_id,
                category_name=transaction.category_name,
            )
            if transaction.transaction_type == INCOME_TRANSACTION_TYPE:
                inserted_row = connection.execute(
                    INCOMES.insert()
                    .values(
                        user_id=user_id,
                        category_id=category_id,
                        amount=transaction.amount,
                        description=transaction.description,
                        created_at=transaction.created_at,
                    )
                    .returning(INCOMES.c.id, INCOMES.c.created_at),
                ).one()
            else:
                inserted_row = connection.execute(
                    TRANSACTIONS.insert()
                    .values(
                        user_id=user_id,
                        category_id=category_id,
                        amount=transaction.amount,
                        transaction_type=EXPENSE_TRANSACTION_TYPE,
                        description=transaction.description,
                        created_at=transaction.created_at,
                    )
                    .returning(TRANSACTIONS.c.id, TRANSACTIONS.c.created_at),
                ).one()

        return TransactionRecord(
            id=inserted_row.id,
            user_id=user_id,
            telegram_id=telegram_id,
            category_id=category_id,
            category_name=transaction.category_name,
            transaction_type=transaction.transaction_type,
            amount=transaction.amount,
            description=transaction.description,
            created_at=inserted_row.created_at,
        )

    def _resolve_user(
        self,
        *,
        connection: Connection,
        telegram_id: int | None,
    ) -> tuple[int, int]:
        if telegram_id is not None:
            user_id = self._get_or_create_user_id(
                connection=connection,
                telegram_id=telegram_id,
            )
            return user_id, telegram_id

        existing_user = connection.execute(
            select(USERS.c.id, USERS.c.telegram_id).order_by(USERS.c.id.asc()).limit(1),
        ).first()
        if existing_user is not None:
            return int(existing_user.id), int(existing_user.telegram_id)

        default_user_id = self._get_or_create_user_id(
            connection=connection,
            telegram_id=DEFAULT_WEB_TELEGRAM_ID,
        )
        return default_user_id, DEFAULT_WEB_TELEGRAM_ID

    def delete_transaction(
        self,
        *,
        transaction_id: int,
        transaction_type: str | None = None,
    ) -> DeletedTransaction:
        """Delete an income or expense row by id, rejecting missing or ambiguous ids."""
        with self._engine.begin() as connection:
            resolved_type = self._resolve_delete_target_type(
                connection=connection,
                transaction_id=transaction_id,
                requested_type=transaction_type,
            )
            target_table = (
                INCOMES if resolved_type == INCOME_TRANSACTION_TYPE else TRANSACTIONS
            )
            deleted_row = connection.execute(
                target_table.delete()
                .where(target_table.c.id == transaction_id)
                .returning(target_table.c.id),
            ).first()
            if deleted_row is None:
                raise TransactionNotFoundError(
                    f"Транзакцію з id={transaction_id} не знайдено в базі даних.",
                )

        return DeletedTransaction(
            id=int(deleted_row.id),
            transaction_type=resolved_type,
        )

    def _resolve_delete_target_type(
        self,
        *,
        connection: Connection,
        transaction_id: int,
        requested_type: str | None,
    ) -> str:
        if requested_type is not None:
            return requested_type

        has_expense = (
            connection.execute(
                select(TRANSACTIONS.c.id).where(TRANSACTIONS.c.id == transaction_id),
            ).first()
            is not None
        )
        has_income = (
            connection.execute(
                select(INCOMES.c.id).where(INCOMES.c.id == transaction_id),
            ).first()
            is not None
        )
        if has_expense and has_income:
            raise TransactionConflictError(
                f"Знайдено і витрату, і дохід з id={transaction_id}. "
                "Вкажіть параметр ?type=expense або ?type=income.",
            )
        if not has_expense and not has_income:
            raise TransactionNotFoundError(
                f"Транзакцію з id={transaction_id} не знайдено в базі даних.",
            )
        return INCOME_TRANSACTION_TYPE if has_income else EXPENSE_TRANSACTION_TYPE
