from app.ai import (
    DEFAULT_GEMINI_MODEL,
    GeminiConfigurationError,
    GeminiModelUnavailableError,
    load_gemini_credentials,
    request_gemini_text,
)


TEST_PROMPT = "Відповідай одним реченням українською: підтверди, що модель працює."
SUCCESS_MESSAGE = "OK"
FAILURE_MESSAGE = "Not OK"
SUCCESS_EXIT_CODE = 0
FAILURE_EXIT_CODE = 1


def main() -> int:
    try:
        auth_key, configured_model = load_gemini_credentials()
        result = request_gemini_text(
            auth_key=auth_key,
            model=configured_model or DEFAULT_GEMINI_MODEL,
            prompt=TEST_PROMPT,
        )
    except (GeminiConfigurationError, GeminiModelUnavailableError) as error:
        print(f"{FAILURE_MESSAGE}: {error}")
        return FAILURE_EXIT_CODE

    print(f"{SUCCESS_MESSAGE} ({result.model}): {result.reply_text}")
    return SUCCESS_EXIT_CODE


if __name__ == "__main__":
    raise SystemExit(main())
