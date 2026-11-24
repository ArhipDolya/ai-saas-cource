import json
import os
from dataclasses import dataclass

import tiktoken
from dotenv import load_dotenv
from sqlalchemy.exc import SQLAlchemyError

from app.ai import (
    DEFAULT_GEMINI_MODEL,
    GEMINI_MODEL_VARIABLE,
    MAX_TRANSACTIONS_FOR_ANALYSIS,
    serialize_transactions_for_prompt,
    transaction_to_prompt_dict,
)
from app.database import ENV_PATH, DatabaseConfigurationError, create_database_engine
from app.expenses import ExpenseService, TransactionRecord
from app.prompts import (
    PromptTemplateError,
    build_transaction_analysis_prompt,
    load_finance_analyze_prompt_template,
)


TIKTOKEN_ENCODING_NAME = "o200k_base"  # Modern BPE encoding used to estimate tokens.
SUCCESS_EXIT_CODE = 0
FAILURE_EXIT_CODE = 1


@dataclass(frozen=True, kw_only=True)
class TransactionTokenBreakdown:
    transaction_id: int
    transaction_type: str
    amount: float
    category_name: str
    tokens: int


@dataclass(frozen=True, kw_only=True)
class TokenCostReport:
    model: str
    encoding_name: str
    transaction_count: int
    transactions_tokens: int
    prompt_template_tokens: int
    full_prompt_tokens: int
    per_transaction_tokens: list[TransactionTokenBreakdown]


def main() -> int:
    try:
        report = measure_database_transactions_tokens()
    except (
        DatabaseConfigurationError,
        SQLAlchemyError,
        PromptTemplateError,
        ValueError,
    ) as error:
        print(f"Not OK: {type(error).__name__}: {error}")
        return FAILURE_EXIT_CODE

    print_token_report(report)
    return SUCCESS_EXIT_CODE


def measure_database_transactions_tokens() -> TokenCostReport:
    """Fetch transactions from PostgreSQL and count their tokens with tiktoken."""
    load_dotenv(dotenv_path=ENV_PATH)
    model = os.getenv(GEMINI_MODEL_VARIABLE, DEFAULT_GEMINI_MODEL).strip()
    target_model = model or DEFAULT_GEMINI_MODEL

    engine = create_database_engine()
    try:
        transactions = ExpenseService(engine).list_transactions()
    finally:
        engine.dispose()

    return build_token_cost_report(
        transactions=transactions,
        model=target_model,
        encoding_name=TIKTOKEN_ENCODING_NAME,
    )


def build_token_cost_report(
    *,
    transactions: list[TransactionRecord],
    model: str,
    encoding_name: str = TIKTOKEN_ENCODING_NAME,
) -> TokenCostReport:
    """Compute tiktoken counts for transactions JSON, prompt template, and total."""
    encoding = tiktoken.get_encoding(encoding_name)
    serialized_transactions = serialize_transactions_for_prompt(transactions)
    prompt_template = load_finance_analyze_prompt_template()
    full_prompt = build_transaction_analysis_prompt(serialized_transactions)

    per_transaction_tokens = [
        TransactionTokenBreakdown(
            transaction_id=transaction.id,
            transaction_type=transaction.transaction_type,
            amount=float(transaction.amount),
            category_name=transaction.category_name,
            tokens=count_tokens(
                text=json.dumps(
                    transaction_to_prompt_dict(transaction),
                    ensure_ascii=False,
                ),
                encoding=encoding,
            ),
        )
        for transaction in transactions
    ]

    return TokenCostReport(
        model=model,
        encoding_name=encoding_name,
        transaction_count=len(transactions),
        transactions_tokens=count_tokens(
            text=serialized_transactions,
            encoding=encoding,
        ),
        prompt_template_tokens=count_tokens(
            text=prompt_template,
            encoding=encoding,
        ),
        full_prompt_tokens=count_tokens(
            text=full_prompt,
            encoding=encoding,
        ),
        per_transaction_tokens=per_transaction_tokens,
    )


def count_tokens(
    *,
    text: str,
    encoding: tiktoken.Encoding,
) -> int:
    return len(encoding.encode(text))


def print_token_report(report: TokenCostReport) -> None:
    print(f"Модель: {report.model}")
    print(f"Кодування tiktoken: {report.encoding_name} (оцінка BPE)")
    print(
        f"Транзакцій у базі даних: {report.transaction_count} "
        f"(ліміт на запит: {MAX_TRANSACTIONS_FOR_ANALYSIS})",
    )
    print(f"Токени самих транзакцій (JSON): {report.transactions_tokens}")
    print(f"Токени шаблону промпта: {report.prompt_template_tokens}")
    print(f"Загалом вхідних токенів у промпті: {report.full_prompt_tokens}")

    if report.transaction_count > 0:
        average_tokens = report.transactions_tokens / report.transaction_count
        print(f"Середньо токенів на 1 транзакцію: {average_tokens:.2f}")
        print("\nДеталізація по транзакціях:")
        for item in report.per_transaction_tokens:
            print(
                f"  - [{item.transaction_type}] id={item.transaction_id}, "
                f"{item.amount:.2f} грн ({item.category_name}): {item.tokens} токенів",
            )


if __name__ == "__main__":
    raise SystemExit(main())
