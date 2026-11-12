import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.engine.interfaces import ReflectedForeignKeyConstraint
from sqlalchemy.exc import ArgumentError
from sqlalchemy.schema import AddConstraint

from app.models import (
    CATEGORIES,
    CATEGORY_OWNER_FOREIGN_KEY,
    CATEGORY_OWNER_UNIQUE,
    DATABASE_SCHEMA,
    INCOME_CATEGORY_OWNER_FOREIGN_KEY,
    INCOMES,
    METADATA,
    TRANSACTIONS,
)


ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
DATABASE_URL_VARIABLE = "DATABASE_URL"
POSTGRESQL_DRIVER = "postgresql+psycopg"
CONNECTION_TIMEOUT_SECONDS = 10


class DatabaseConfigurationError(ValueError):
    """The database configuration cannot be used without exposing its value."""


def create_database_engine() -> Engine:
    load_dotenv(dotenv_path=ENV_PATH)
    database_url = os.getenv(DATABASE_URL_VARIABLE, "").strip()
    if not database_url:
        raise DatabaseConfigurationError("Set DATABASE_URL in the environment or .env.")

    try:
        connection_url: URL = make_url(database_url)
    except (ArgumentError, ValueError):
        raise DatabaseConfigurationError(
            "DATABASE_URL has an invalid format.",
        ) from None

    is_postgresql = connection_url.get_backend_name() in {"postgres", "postgresql"}
    if not is_postgresql:
        raise DatabaseConfigurationError("DATABASE_URL must point to PostgreSQL.")

    return create_engine(
        url=connection_url.set(drivername=POSTGRESQL_DRIVER),
        connect_args={"connect_timeout": CONNECTION_TIMEOUT_SECONDS},
        pool_pre_ping=True,
        hide_parameters=True,
    )


def initialize_database(engine: Engine) -> None:
    """Create missing tables and add the diagram's category ownership constraints."""
    with engine.begin() as connection:
        METADATA.create_all(bind=connection)
        inspector = inspect(connection)
        category_constraints = inspector.get_unique_constraints(
            table_name=CATEGORIES.name,
            schema=DATABASE_SCHEMA,
        )
        has_owner_unique = any(
            constraint["column_names"] == ["id", "user_id"]
            for constraint in category_constraints
        )
        if not has_owner_unique:
            connection.execute(
                AddConstraint(
                    element=CATEGORY_OWNER_UNIQUE,
                    isolate_from_table=False,
                ),
            )

        transaction_foreign_keys = inspector.get_foreign_keys(
            table_name=TRANSACTIONS.name,
            schema=DATABASE_SCHEMA,
        )
        has_owner_foreign_key = any(
            _is_category_owner_foreign_key(foreign_key)
            for foreign_key in transaction_foreign_keys
        )
        if not has_owner_foreign_key:
            connection.execute(
                AddConstraint(
                    element=CATEGORY_OWNER_FOREIGN_KEY,
                    isolate_from_table=False,
                ),
            )

        income_foreign_keys = inspector.get_foreign_keys(
            table_name=INCOMES.name,
            schema=DATABASE_SCHEMA,
        )
        has_income_owner_foreign_key = any(
            _is_category_owner_foreign_key(foreign_key)
            for foreign_key in income_foreign_keys
        )
        if not has_income_owner_foreign_key:
            connection.execute(
                AddConstraint(
                    element=INCOME_CATEGORY_OWNER_FOREIGN_KEY,
                    isolate_from_table=False,
                ),
            )

        income_columns = {
            column["name"]
            for column in inspector.get_columns(
                table_name=INCOMES.name,
                schema=DATABASE_SCHEMA,
            )
        }
        has_income_description = "description" in income_columns
        if not has_income_description:
            connection.execute(
                text(
                    "ALTER TABLE public.incomes "
                    "ADD COLUMN IF NOT EXISTS description VARCHAR(255)",
                ),
            )


def _is_category_owner_foreign_key(
    foreign_key: ReflectedForeignKeyConstraint,
) -> bool:
    has_source_columns = foreign_key["constrained_columns"] == [
        "category_id",
        "user_id",
    ]
    has_category_table = foreign_key["referred_table"] == CATEGORIES.name
    has_category_schema = foreign_key["referred_schema"] in {None, DATABASE_SCHEMA}
    has_owner_columns = foreign_key["referred_columns"] == ["id", "user_id"]
    return all(
        (
            has_source_columns,
            has_category_table,
            has_category_schema,
            has_owner_columns,
        ),
    )
