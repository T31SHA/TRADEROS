"""Database engine helpers."""

from typing import Any

from sqlalchemy import Engine, create_engine, event


def create_database_engine(database_url: str, *, echo: bool = False) -> Engine:
    """Create a SQLAlchemy engine; no connection is made until first use."""

    engine = create_engine(database_url, echo=echo, future=True)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection: Any, connection_record: Any) -> None:
            del connection_record
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


__all__ = ["create_database_engine"]
