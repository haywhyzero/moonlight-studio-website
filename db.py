"""SQLite persistence for orders, codes, entitlements, and usage."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    reference TEXT NOT NULL UNIQUE,
    provider TEXT NOT NULL,
    email TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    term TEXT NOT NULL,
    amount_kobo INTEGER NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    claim_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    paid_at TEXT
);

CREATE TABLE IF NOT EXISTS subscription_codes (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL UNIQUE REFERENCES orders(id),
    code_hash TEXT NOT NULL UNIQUE,
    plan_id TEXT NOT NULL,
    term TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unused',
    created_at TEXT NOT NULL,
    redeemed_at TEXT,
    redeemed_device TEXT
);

-- One subscription code can be active on several devices. Each row is a
-- device-bound entitlement with its own signed token, but every device shares
-- the code's start/expiry window and daily usage pool.
CREATE TABLE IF NOT EXISTS entitlements (
    id TEXT PRIMARY KEY,
    code_id TEXT NOT NULL REFERENCES subscription_codes(id),
    order_id TEXT NOT NULL REFERENCES orders(id),
    plan_id TEXT NOT NULL,
    term TEXT NOT NULL,
    device_id TEXT NOT NULL,
    device_name TEXT,
    starts_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    last_usage_at TEXT,
    UNIQUE (code_id, device_id)
);

-- Usage is counted per subscription code, not per device, so three devices
-- cannot multiply the daily allowance.
CREATE TABLE IF NOT EXISTS usage_daily (
    code_id TEXT NOT NULL REFERENCES subscription_codes(id),
    day TEXT NOT NULL,
    seconds INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (code_id, day)
);

CREATE TABLE IF NOT EXISTS usage_events (
    event_id TEXT PRIMARY KEY,
    entitlement_id TEXT NOT NULL REFERENCES entitlements(id),
    code_id TEXT NOT NULL REFERENCES subscription_codes(id),
    day TEXT NOT NULL,
    seconds INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS webhook_events (
    event_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    received_at TEXT NOT NULL,
    payload_hash TEXT NOT NULL
);

-- Activation and checkout throttling. Stored in SQLite so a restart or a
-- second worker process cannot reset an attacker's budget.
CREATE TABLE IF NOT EXISTS rate_limit_events (
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_entitlements_device
    ON entitlements(device_id);
CREATE INDEX IF NOT EXISTS idx_entitlements_code
    ON entitlements(code_id);
CREATE INDEX IF NOT EXISTS idx_usage_daily_day
    ON usage_daily(day);
CREATE INDEX IF NOT EXISTS idx_rate_limit_events
    ON rate_limit_events(scope, key, at);
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            self._migrate_device_limit(connection)
            self._migrate_usage_heartbeat(connection)
            # Re-create tables/indexes after a migration rebuilt them.
            connection.executescript(SCHEMA)
            connection.commit()
            connection.execute("PRAGMA journal_mode = WAL")

    # ------------------------------------------------------------------ migrate
    def _migrate_usage_heartbeat(self, connection: sqlite3.Connection) -> None:
        """Add the server-side heartbeat timestamp to existing installs.

        SQLite has no `ADD COLUMN IF NOT EXISTS`, so the column list is checked
        first. Older databases simply start with no previous heartbeat.
        """
        if "last_usage_at" in _columns(connection, "entitlements"):
            return
        connection.execute(
            "ALTER TABLE entitlements ADD COLUMN last_usage_at TEXT"
        )

    def _migrate_device_limit(self, connection: sqlite3.Connection) -> None:
        """Move pre-multi-device databases to the shared-subscription schema.

        Early builds stored exactly one entitlement per code (`code_id UNIQUE`)
        and counted usage per entitlement. Both assumptions are wrong for
        multi-device plans, so those tables are rebuilt. Orders and issued
        codes are preserved; per-device usage counters are intentionally reset
        because they cannot be attributed to a shared pool.
        """
        entitlements_sql = _table_sql(connection, "entitlements")
        usage_columns = _columns(connection, "usage_daily")

        needs_entitlement_rebuild = bool(
            entitlements_sql and "code_id TEXT NOT NULL UNIQUE" in entitlements_sql
        )
        needs_usage_rebuild = bool(
            usage_columns and "code_id" not in usage_columns
        )
        if not needs_entitlement_rebuild and not needs_usage_rebuild:
            return

        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.execute("BEGIN")
            if needs_usage_rebuild:
                connection.execute("DROP TABLE IF EXISTS usage_events")
                connection.execute("DROP TABLE IF EXISTS usage_daily")
            if needs_entitlement_rebuild:
                connection.execute(
                    """
                    CREATE TABLE entitlements_new (
                        id TEXT PRIMARY KEY,
                        code_id TEXT NOT NULL REFERENCES subscription_codes(id),
                        order_id TEXT NOT NULL REFERENCES orders(id),
                        plan_id TEXT NOT NULL,
                        term TEXT NOT NULL,
                        device_id TEXT NOT NULL,
                        device_name TEXT,
                        starts_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'active',
                        created_at TEXT NOT NULL,
                        UNIQUE (code_id, device_id)
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO entitlements_new (
                        id, code_id, order_id, plan_id, term, device_id,
                        device_name, starts_at, expires_at, status, created_at
                    )
                    SELECT
                        id, code_id, order_id, plan_id, term, device_id,
                        device_name, starts_at, expires_at, status, created_at
                    FROM entitlements
                    """
                )
                connection.execute("DROP TABLE entitlements")
                connection.execute(
                    "ALTER TABLE entitlements_new RENAME TO entitlements"
                )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")


def _table_sql(connection: sqlite3.Connection, table: str) -> str | None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return str(row["sql"]) if row and row["sql"] else None


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row["name"]) for row in rows}
