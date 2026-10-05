import os
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker


# Always anchor Rawaj's local SQLite database at the repository root.  The
# outreach notebook runs from a nested directory, so a bare relative URL would
# otherwise silently create a second, empty `rawaj.db` beside the notebook.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env", override=False)

DEFAULT_DATABASE_URL = f"sqlite:///{(PROJECT_ROOT / 'rawaj.db').as_posix()}"


def _resolve_database_url(configured_url: str | None) -> str:
    """Keep the legacy relative local URL compatible and deterministic."""
    if configured_url in {None, "", "sqlite:///./rawaj.db", "sqlite:///rawaj.db"}:
        return DEFAULT_DATABASE_URL
    return configured_url


DATABASE_URL = _resolve_database_url(os.getenv("DATABASE_URL"))


engine = create_engine(
    DATABASE_URL,
    echo=False,
)


def ensure_legacy_database_schema(database_engine: object = engine) -> None:
    """Fill in SQLite columns that were missing from older local databases."""
    with database_engine.begin() as connection:
        inspector = inspect(connection)
        if "strategies" in inspector.get_table_names():
            strategy_columns = {column["name"] for column in inspector.get_columns("strategies")}
            if "restaurant_name" not in strategy_columns:
                connection.execute(text("ALTER TABLE strategies ADD COLUMN restaurant_name VARCHAR(255)"))
                # Strategies saved by the Strategy Agent carry the name in their result.
                connection.execute(text(
                    "UPDATE strategies SET restaurant_name = json_extract(strategy_data, '$.restaurant') "
                    "WHERE restaurant_name IS NULL AND json_valid(strategy_data)"
                ))
        if "restaurant_contexts" not in inspector.get_table_names():
            return
        columns = {column["name"] for column in inspector.get_columns("restaurant_contexts")}
        if "created_at" not in columns:
            connection.execute(text("ALTER TABLE restaurant_contexts ADD COLUMN created_at DATETIME"))
        if "updated_at" not in columns:
            connection.execute(text("ALTER TABLE restaurant_contexts ADD COLUMN updated_at DATETIME"))
        connection.execute(text(
            "UPDATE restaurant_contexts SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"
        ))
        connection.execute(text(
            "UPDATE restaurant_contexts SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL"
        ))


SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()

    try:
        yield db
    finally:
        db.close()
