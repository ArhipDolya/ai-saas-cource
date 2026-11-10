from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.database import create_database_engine


HEALTH_CHECK_QUERY = "SELECT 1"
EXPECTED_HEALTH_CHECK_RESULT = 1
SUCCESS_MESSAGE = "OK"
FAILURE_MESSAGE = "Not OK"
SUCCESS_EXIT_CODE = 0
FAILURE_EXIT_CODE = 1


def main() -> int:
    is_connected = is_database_connected()
    print(SUCCESS_MESSAGE if is_connected else FAILURE_MESSAGE)
    return SUCCESS_EXIT_CODE if is_connected else FAILURE_EXIT_CODE


def is_database_connected() -> bool:
    try:
        engine = create_database_engine()
        try:
            with engine.connect() as connection:
                return (
                    connection.scalar(text(HEALTH_CHECK_QUERY))
                    == EXPECTED_HEALTH_CHECK_RESULT
                )
        finally:
            engine.dispose()
    except (SQLAlchemyError, ValueError, TypeError):
        return False


if __name__ == "__main__":
    raise SystemExit(main())
