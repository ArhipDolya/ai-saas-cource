from pathlib import Path


PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
FINANCE_ANALYZE_PROMPT_FILENAME = "finance_analyze_prompt.txt"
FINANCE_ANALYZE_PROMPT_PATH = PROMPTS_DIR / FINANCE_ANALYZE_PROMPT_FILENAME
PROMPT_ENCODING = "utf-8"
TRANSACTIONS_DATA_MARKER = "__TRANSACTIONS_JSON__"


class PromptTemplateError(RuntimeError):
    """The prompt template file cannot be loaded or is missing its data marker."""


def build_transaction_analysis_prompt(transactions_json: str) -> str:
    """Load the prompt template from disk and insert serialized transactions JSON."""
    template = load_finance_analyze_prompt_template()
    return template.replace(
        TRANSACTIONS_DATA_MARKER,
        transactions_json,
    )


def load_finance_analyze_prompt_template() -> str:
    """Read the finance analysis prompt text from the prompts directory."""
    if not FINANCE_ANALYZE_PROMPT_PATH.is_file():
        raise PromptTemplateError(
            f"Prompt template file not found: {FINANCE_ANALYZE_PROMPT_PATH}",
        )

    template_text = FINANCE_ANALYZE_PROMPT_PATH.read_text(encoding=PROMPT_ENCODING)
    if TRANSACTIONS_DATA_MARKER not in template_text:
        raise PromptTemplateError(
            f"Prompt template is missing the '{TRANSACTIONS_DATA_MARKER}' placeholder.",
        )
    return template_text
