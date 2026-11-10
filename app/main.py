import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message
from dotenv import load_dotenv
from sqlalchemy.exc import SQLAlchemyError

from app.database import (
    ENV_PATH,
    DatabaseConfigurationError,
    create_database_engine,
    initialize_database,
)
from app.expenses import ExpenseService, ExpenseValidationError, parse_expense


BOT_TOKEN_VARIABLE = "BOT_TOKEN"
START_GREETING = "Привіт! Бот працює 👋"
HELP_COMMAND = "help"
HELP_MESSAGE = "Це фінансовий бот 💰\nДодати витрату: /expense 120 кава"
EXPENSE_COMMAND = "expense"
LOGGER = logging.getLogger(__name__)


async def handle_start(message: Message) -> None:
    await message.answer(text=START_GREETING)


async def handle_help(message: Message) -> None:
    await message.answer(text=HELP_MESSAGE)


async def handle_expense(
    message: Message,
    *,
    command: CommandObject,
    expense_service: ExpenseService,
) -> None:
    sender = message.from_user
    if sender is None:
        await message.answer(
            text="Не вдалося визначити користувача. Напиши боту особисто.",
        )
        return

    try:
        expense_input = parse_expense(command_args=command.args)
    except ExpenseValidationError as error:
        await message.answer(text=str(error))
        return

    try:
        expense = await asyncio.to_thread(
            expense_service.create_expense,
            telegram_id=sender.id,
            expense=expense_input,
        )
    except SQLAlchemyError as error:
        LOGGER.warning(
            "Expense creation failed",
            extra={"telegram_id": sender.id, "error_type": type(error).__name__},
        )
        await message.answer(text="Не вдалося зберегти витрату. Спробуй пізніше.")
        return

    await message.answer(
        text=f"Витрату створено: {expense.amount:.2f} грн, категорія: {expense.category_name}.",
    )


async def main() -> None:
    load_dotenv(dotenv_path=ENV_PATH)
    bot_token: str | None = os.getenv(BOT_TOKEN_VARIABLE)
    if not bot_token:
        raise ValueError("Set BOT_TOKEN in the environment or the project .env file.")

    engine = create_database_engine()
    try:
        await asyncio.to_thread(initialize_database, engine=engine)
        dispatcher = create_dispatcher(expense_service=ExpenseService(engine))

        async with Bot(token=bot_token) as bot:
            LOGGER.info("Бот почав роботу")
            await dispatcher.start_polling(bot)
    finally:
        await asyncio.to_thread(engine.dispose)


def create_dispatcher(expense_service: ExpenseService) -> Dispatcher:
    dispatcher = Dispatcher(expense_service=expense_service)
    dispatcher.message.register(handle_start, CommandStart())
    dispatcher.message.register(handle_help, Command(HELP_COMMAND))
    dispatcher.message.register(handle_expense, Command(EXPENSE_COMMAND))
    return dispatcher


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(main())
    except (DatabaseConfigurationError, SQLAlchemyError) as error:
        LOGGER.error(
            "Database startup failed",
            extra={"error_type": type(error).__name__},
        )
        raise SystemExit(
            "Cannot start bot. Check DATABASE_URL and database availability.",
        ) from None
