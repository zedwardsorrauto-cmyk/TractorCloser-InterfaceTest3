"""Small, versioned schema upgrades for the TractorCloser pilot.

Each upgrade is recorded once in ``schema_migrations``. This replaces the
untracked startup alterations used during the early prototype and gives every
future database change an explicit version and release note.
"""

from collections.abc import Callable

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


def _add_missing_columns(engine: Engine, table: str, columns: dict[str, str]) -> None:
    existing = {column["name"] for column in inspect(engine).get_columns(table)}
    with engine.begin() as connection:
        for name, definition in columns.items():
            if name not in existing:
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))


def _pilot_schema_upgrade(engine: Engine) -> None:
    _add_missing_columns(engine, "leads", {
        "assigned_user_id": "INTEGER",
        "source": "VARCHAR(100) DEFAULT 'Manual'",
        "source_reference": "VARCHAR(240) DEFAULT ''",
        "external_source_id": "VARCHAR(240) DEFAULT ''",
        "contact_consent": "VARCHAR(40) DEFAULT 'Unknown'",
        "preferred_contact_channel": "VARCHAR(40) DEFAULT ''",
        "original_inquiry": "TEXT DEFAULT ''",
        "response_sent": "BOOLEAN DEFAULT FALSE",
    })
    _add_missing_columns(engine, "lead_activities", {"actor_user_id": "INTEGER"})
    _add_missing_columns(engine, "users", {
        "session_version": "INTEGER DEFAULT 1",
        "must_change_password": "BOOLEAN DEFAULT FALSE",
    })
    _add_missing_columns(engine, "deals", {"sold_by_user_id": "INTEGER"})


def _push_subscription_upgrade(engine: Engine) -> None:
    # The model metadata creates the table for new deployments.  Keeping this
    # migration gives existing pilot databases an explicit, one-time upgrade.
    from .database import Base
    Base.metadata.tables["push_subscriptions"].create(bind=engine, checkfirst=True)


MIGRATIONS: list[tuple[int, str, Callable[[Engine], None]]] = [
    (1, "pilot_schema_baseline", _pilot_schema_upgrade),
    (2, "browser_push_subscriptions", _push_subscription_upgrade),
]


def run_schema_migrations(engine: Engine) -> list[int]:
    """Apply every missing migration and return versions applied this startup."""
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name VARCHAR(160) NOT NULL, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"))
        completed = {row[0] for row in connection.execute(text("SELECT version FROM schema_migrations"))}
    applied: list[int] = []
    for version, name, upgrade in MIGRATIONS:
        if version in completed:
            continue
        upgrade(engine)
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO schema_migrations (version, name) VALUES (:version, :name)"), {"version": version, "name": name})
        applied.append(version)
    return applied


def current_schema_version(engine: Engine) -> int:
    if "schema_migrations" not in inspect(engine).get_table_names():
        return 0
    with engine.connect() as connection:
        return int(connection.execute(text("SELECT COALESCE(MAX(version), 0) FROM schema_migrations")).scalar_one())
