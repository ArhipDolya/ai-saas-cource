from datetime import date
from pathlib import Path


PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
FINANCE_ANALYZE_PROMPT_FILENAME = "finance_analyze_prompt.txt"
FINANCE_ANALYZE_PROMPT_PATH = PROMPTS_DIR / FINANCE_ANALYZE_PROMPT_FILENAME
FINANCE_CHAT_PROMPT_FILENAME = "finance_chat_prompt.txt"
FINANCE_CHAT_PROMPT_PATH = PROMPTS_DIR / FINANCE_CHAT_PROMPT_FILENAME
PROMPT_ENCODING = "utf-8"
TRANSACTIONS_DATA_MARKER = "__TRANSACTIONS_JSON__"
CURRENT_DATE_MARKER = "__CURRENT_DATE__"


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
    return _load_prompt_template(
        path=FINANCE_ANALYZE_PROMPT_PATH,
        required_markers=(TRANSACTIONS_DATA_MARKER,),
    )


def build_project_chat_system_prompt(
    *,
    current_date: str | None = None,
) -> str:
    """Load the chat prompt template with tool usage guidance and current date."""
    template = load_finance_chat_prompt_template()
    resolved_date = current_date or date.today().isoformat()
    return template.replace(CURRENT_DATE_MARKER, resolved_date)


def load_finance_chat_prompt_template() -> str:
    """Read the project AI chat system prompt text from the prompts directory."""
    return _load_prompt_template(
        path=FINANCE_CHAT_PROMPT_PATH,
        required_markers=(CURRENT_DATE_MARKER,),
    )


def _load_prompt_template(
    *,
    path: Path,
    required_markers: tuple[str, ...],
) -> str:
    if not path.is_file():
        raise PromptTemplateError(f"Prompt template file not found: {path}")

    template_text = path.read_text(encoding=PROMPT_ENCODING)
    for marker in required_markers:
        if marker not in template_text:
            raise PromptTemplateError(
                f"Prompt template is missing the '{marker}' placeholder.",
            )
    return template_text
