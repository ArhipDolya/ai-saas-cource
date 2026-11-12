from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    MetaData,
    Numeric,
    String,
    Table,
    UniqueConstraint,
    func,
)


CATEGORY_NAME_MAX_LENGTH = 100
TRANSACTION_TYPE_MAX_LENGTH = 10
DESCRIPTION_MAX_LENGTH = 255
EXPENSE_TRANSACTION_TYPE = "expense"
INCOME_TRANSACTION_TYPE = "income"
AMOUNT_PRECISION = 12
AMOUNT_SCALE = 2
DATABASE_SCHEMA = "public"
METADATA = MetaData(schema=DATABASE_SCHEMA)

USERS = Table(
    "users",
    METADATA,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("telegram_id", BigInteger, nullable=False, unique=True),
    Column("created_at", DateTime, nullable=False, server_default=func.now()),
)

CATEGORY_OWNER_UNIQUE = UniqueConstraint(
    "id",
    "user_id",
    name="uq_categories_id_user",
)
CATEGORIES = Table(
    "categories",
    METADATA,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column(
        "user_id",
        BigInteger,
        ForeignKey("public.users.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("name", String(CATEGORY_NAME_MAX_LENGTH), nullable=False),
    Column("created_at", DateTime, nullable=False, server_default=func.now()),
    UniqueConstraint("user_id", "name", name="uq_categories_user_name"),
    CATEGORY_OWNER_UNIQUE,
)

CATEGORY_OWNER_FOREIGN_KEY = ForeignKeyConstraint(
    ["category_id", "user_id"],
    ["public.categories.id", "public.categories.user_id"],
    name="fk_transactions_category_owner",
    ondelete="RESTRICT",
)
TRANSACTIONS = Table(
    "transactions",
    METADATA,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column(
        "user_id",
        BigInteger,
        ForeignKey("public.users.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("category_id", BigInteger, nullable=False),
    Column(
        "amount",
        Numeric(precision=AMOUNT_PRECISION, scale=AMOUNT_SCALE),
        nullable=False,
    ),
    Column(
        "transaction_type",
        String(TRANSACTION_TYPE_MAX_LENGTH),
        nullable=False,
        server_default=EXPENSE_TRANSACTION_TYPE,
    ),
    Column("description", String(DESCRIPTION_MAX_LENGTH), nullable=True),
    Column("created_at", DateTime, nullable=False, server_default=func.now()),
    CheckConstraint("amount > 0", name="transactions_amount_positive"),
    CATEGORY_OWNER_FOREIGN_KEY,
)

INCOME_CATEGORY_OWNER_FOREIGN_KEY = ForeignKeyConstraint(
    ["category_id", "user_id"],
    ["public.categories.id", "public.categories.user_id"],
    name="fk_incomes_category_owner",
    ondelete="RESTRICT",
)
INCOMES = Table(
    "incomes",
    METADATA,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column(
        "user_id",
        BigInteger,
        ForeignKey("public.users.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("category_id", BigInteger, nullable=False),
    Column(
        "amount",
        Numeric(precision=AMOUNT_PRECISION, scale=AMOUNT_SCALE),
        nullable=False,
    ),
    Column("description", String(DESCRIPTION_MAX_LENGTH), nullable=True),
    Column("created_at", DateTime, nullable=False, server_default=func.now()),
    CheckConstraint("amount > 0", name="incomes_amount_positive"),
    INCOME_CATEGORY_OWNER_FOREIGN_KEY,
)
