import re
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection, Engine

from app.models import CATEGORIES, CATEGORY_NAME_MAX_LENGTH, TRANSACTIONS, USERS


EXPENSE_USAGE = "Використай команду: /expense 120 кава"
AMOUNT_PATTERN = re.compile(r"[0-9]+(?:[.,][0-9]{1,2})?")
MINIMUM_EXPENSE_AMOUNT = Decimal("0.01")
MAXIMUM_EXPENSE_AMOUNT = Decimal("9999999999.99")  # NUMERIC(12, 2).
AMOUNT_QUANTUM = Decimal("0.01")
EXPENSE_ARGUMENT_COUNT = 2
CATEGORY_SPLIT_LIMIT = 1  # The category may contain multiple words.


class ExpenseValidationError(ValueError):
    """The expense cannot be stored without changing the user's input."""


@dataclass(frozen=True, kw_only=True)
class ExpenseInput:
    amount: Decimal
    category_name: str


@dataclass(frozen=True, kw_only=True)
class CreatedExpense:
    transaction_id: int
    amount: Decimal
    category_name: str


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
