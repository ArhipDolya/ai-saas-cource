import asyncio
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
BOT_TOKEN_VARIABLE = "BOT_TOKEN"
START_GREETING = "Привіт! Бот працює 👋"
HELP_COMMAND = "help"
HELP_MESSAGE = "Це фінансовий бот 💰"


async def handle_start(message: Message) -> None:
    await message.answer(text=START_GREETING)


async def handle_help(message: Message) -> None:
    await message.answer(text=HELP_MESSAGE)


async def main() -> None:
    load_dotenv(dotenv_path=ENV_PATH)
    bot_token: str | None = os.getenv(BOT_TOKEN_VARIABLE)
    if not bot_token:
        raise ValueError("Set BOT_TOKEN in the environment or the project .env file.")

    dispatcher = Dispatcher()
    dispatcher.message.register(handle_start, CommandStart())
    dispatcher.message.register(handle_help, Command(HELP_COMMAND))

    async with Bot(token=bot_token) as bot:
        await dispatcher.start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
