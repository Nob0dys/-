from __future__ import annotations

import os
import re
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateTable


class Base(DeclarativeBase):
    pass


DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/quote.db")

if DATABASE_URL.startswith("sqlite"):
    if ":memory:" not in DATABASE_URL:
        db_path = DATABASE_URL.split("///", 1)[-1]
        Path(db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    # 保持 SQLAlchemy 默认事务管理（isolation_level 默认），PRAGMA 通过
    # connect 事件在每个新连接上设置——WAL+NORMAL 提升批量写入性能，
    # 同时不破坏事务批处理（否则每条 INSERT 独立提交反而更慢）。
    connect_args = {"check_same_thread": False, "timeout": 30}
    engine_kwargs = {"connect_args": connect_args}
    if ":memory:" in DATABASE_URL:
        engine_kwargs["poolclass"] = StaticPool
else:
    engine_kwargs = {"pool_pre_ping": True, "pool_recycle": 1800}

engine = create_engine(DATABASE_URL, **engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    """连接级 PRAGMA：WAL 日志 + NORMAL 同步 + 大缓存（批量写入性能关键）。
    通过 connect 事件对每个新建连接生效，不改变 SQLAlchemy 事务语义。"""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA cache_size=-8192")
    finally:
        cursor.close()


if DATABASE_URL.startswith("sqlite") and ":memory:" not in DATABASE_URL:
    from sqlalchemy import event as _sqlalchemy_event

    _sqlalchemy_event.listen(engine, "connect", _set_sqlite_pragmas)


def _build_engine(url: str):
    """Create engine + sessionmaker for a given DATABASE_URL (used by switch_engine)."""
    global engine, SessionLocal
    if url.startswith("sqlite"):
        if ":memory:" not in url:
            db_path = url.split("///", 1)[-1]
            Path(db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        connect_args = {"check_same_thread": False, "timeout": 30}
        engine_kwargs = {"connect_args": connect_args}
        if ":memory:" in url:
            engine_kwargs["poolclass"] = StaticPool
    else:
        engine_kwargs = {"pool_pre_ping": True, "pool_recycle": 1800}
    engine = create_engine(url, **engine_kwargs)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    if url.startswith("sqlite") and ":memory:" not in url:
        from sqlalchemy import event as _sqlalchemy_event

        _sqlalchemy_event.listen(engine, "connect", _set_sqlite_pragmas)


def switch_engine(db_name: str) -> str:
    """热切换当前数据库（data 目录下的 <db_name>.db）。

    新建/切换后自动建表（init_db），调用方应随后执行 seed_database()
    确保 admin/quote 用户与示例客户存在。
    """
    global DATABASE_URL
    if not re.fullmatch(r"[A-Za-z0-9_-]+", db_name):
        raise ValueError("数据库名只能包含字母、数字、下划线、连字符")
    url = f"sqlite:///./data/{db_name}.db"
    _build_engine(url)
    DATABASE_URL = url
    init_db()
    return url


# Columns added to quote_options for manual (history-free) quote options.
MANUAL_OPTION_COLUMNS = {
    "manual_brand": "VARCHAR(300)",
    "manual_manufacturer": "VARCHAR(500)",
    "manual_model": "VARCHAR(300)",
    "manual_spec": "TEXT",
    "manual_unit": "VARCHAR(80)",
}


def init_db() -> None:
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _apply_lightweight_migrations()
    _apply_round3_migrations()


def _apply_round3_migrations() -> None:
    """Round-3 lightweight migrations: add columns introduced for confirm-driven
    history writeback."""
    if not DATABASE_URL.startswith("sqlite"):
        return
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()
        with connection.begin():
            options = _sqlite_columns(connection, "quote_options")
            if options and "recorded_final_price" not in options:
                connection.exec_driver_sql(
                    "ALTER TABLE quote_options ADD COLUMN recorded_final_price FLOAT"
                )
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()


def _sqlite_columns(connection, table: str) -> dict:
    """Map column name -> PRAGMA table_info row (cid, name, type, notnull, dflt, pk)."""
    return {row[1]: row for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")}


def _sqlite_rebuild_table(connection, table_name: str, model_table) -> None:
    """Rebuild a SQLite table from the current model schema, preserving data.

    SQLite cannot relax a NOT NULL constraint in place, so the table is
    recreated: build ``<name>_new`` from the model DDL, copy the columns that
    exist in both schemas, swap names and recreate the indexes.
    """
    old_columns = _sqlite_columns(connection, table_name)
    common = [column.name for column in model_table.columns if column.name in old_columns]
    column_list = ", ".join(common)
    ddl = str(CreateTable(model_table).compile(engine)).strip().rstrip(";")
    ddl = ddl.replace(f"CREATE TABLE {table_name}", f"CREATE TABLE {table_name}_new", 1)
    connection.exec_driver_sql(ddl)
    connection.exec_driver_sql(
        f"INSERT INTO {table_name}_new ({column_list}) SELECT {column_list} FROM {table_name}"
    )
    connection.exec_driver_sql(f"DROP TABLE {table_name}")
    connection.exec_driver_sql(f"ALTER TABLE {table_name}_new RENAME TO {table_name}")
    for index in model_table.indexes:
        index.create(bind=connection, checkfirst=True)


def _apply_lightweight_migrations() -> None:
    """Bring databases created by older versions up to the current schema."""
    from . import models

    if DATABASE_URL.startswith("sqlite"):
        with engine.connect() as connection:
            # FK enforcement is off by default in SQLite; keep it off (the app
            # relies on that default) and run the rebuilds with it off.  The
            # PRAGMA must run outside a transaction, hence the commit first.
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()
            with connection.begin():
                jobs = _sqlite_columns(connection, "quote_jobs")
                if jobs:
                    if jobs["customer_id"][3]:
                        # NOT NULL customer_id -> rebuild (also adds display_name)
                        _sqlite_rebuild_table(connection, "quote_jobs", models.QuoteJob.__table__)
                    elif "display_name" not in jobs:
                        connection.exec_driver_sql(
                            "ALTER TABLE quote_jobs ADD COLUMN display_name VARCHAR(300)"
                        )
                    if "tax_rate" not in jobs:
                        connection.exec_driver_sql(
                            "ALTER TABLE quote_jobs ADD COLUMN tax_rate FLOAT DEFAULT 0.1"
                        )
                options = _sqlite_columns(connection, "quote_options")
                if options:
                    if options["history_quote_id"][3]:
                        # NOT NULL history_quote_id -> rebuild (also adds manual_* columns)
                        _sqlite_rebuild_table(connection, "quote_options", models.QuoteOption.__table__)
                    else:
                        for name, ddl_type in MANUAL_OPTION_COLUMNS.items():
                            if name not in options:
                                connection.exec_driver_sql(
                                    f"ALTER TABLE quote_options ADD COLUMN {name} {ddl_type}"
                                )
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()
        return
    with engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE quote_jobs ADD COLUMN IF NOT EXISTS display_name VARCHAR(300)")
        connection.exec_driver_sql("ALTER TABLE quote_jobs ADD COLUMN IF NOT EXISTS tax_rate FLOAT DEFAULT 0.1")
        connection.exec_driver_sql("ALTER TABLE quote_jobs ALTER COLUMN customer_id DROP NOT NULL")
        connection.exec_driver_sql("ALTER TABLE quote_options ALTER COLUMN history_quote_id DROP NOT NULL")
        for name, ddl_type in MANUAL_OPTION_COLUMNS.items():
            connection.exec_driver_sql(
                f"ALTER TABLE quote_options ADD COLUMN IF NOT EXISTS {name} {ddl_type}"
            )


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
