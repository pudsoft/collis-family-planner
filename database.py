"""
MySQL connection layer with a sqlite3-compatible interface.

Used when DB_DRIVER=mysql (OCI production). Local dev uses sqlite3 directly
via the existing path in app.py.

The MySQLCompat wrapper transparently:
  - Translates ? → %s  (positional params)
  - Translates :name → %(name)s  (named params with dict)
  - Intercepts SELECT last_insert_rowid() and returns the real last ID
  - Returns Row objects (dict subclass) supporting both dict-key and int-index access
"""
from __future__ import annotations

import re

try:
    import pymysql
    import pymysql.cursors
except ImportError:
    pymysql = None  # type: ignore

import config


class Row(dict):
    """Dict subclass that also supports integer indexing for sqlite3 compat."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class _Cursor:
    def __init__(self, cur, last_id: int):
        self._cur = cur
        self._last_id = last_id

    @property
    def lastrowid(self) -> int:
        return self._last_id

    def fetchone(self):
        row = self._cur.fetchone()
        return Row(row) if row is not None else None

    def fetchall(self) -> list[Row]:
        return [Row(r) for r in self._cur.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class _FakeLastIdCursor:
    """Returned when SELECT last_insert_rowid() is intercepted."""

    def __init__(self, last_id: int):
        self._id = last_id

    def fetchone(self):
        return Row({"last_insert_rowid()": self._id})

    def fetchall(self):
        return [Row({"last_insert_rowid()": self._id})]


class MySQLCompat:
    """sqlite3-compatible wrapper around a PyMySQL connection."""

    def __init__(self, conn):
        self._conn = conn
        self._last_id: int = 0

    @staticmethod
    def _adapt(sql: str, params) -> str:
        if isinstance(params, dict):
            sql = re.sub(r":(\w+)", r"%(\1)s", sql)
        else:
            sql = sql.replace("?", "%s")
        # Translate SQLite-only DML to MySQL equivalents
        # "INSERT OR REPLACE INTO t" → "REPLACE INTO t"  (drop INSERT, keep INTO)
        sql = re.sub(r"\bINSERT\s+OR\s+REPLACE\b", "REPLACE", sql, flags=re.IGNORECASE)
        # "INSERT OR IGNORE INTO t"  → "INSERT IGNORE INTO t"
        sql = re.sub(r"\bINSERT\s+OR\s+IGNORE\b",  "INSERT IGNORE", sql, flags=re.IGNORECASE)
        return sql

    def execute(self, sql: str, params=()):
        if "last_insert_rowid()" in sql.lower():
            return _FakeLastIdCursor(self._last_id)

        adapted = self._adapt(sql, params)
        cur = self._conn.cursor()
        cur.execute(adapted, params if params else ())
        self._last_id = cur.lastrowid or self._last_id
        return _Cursor(cur, self._last_id)

    def executemany(self, sql: str, params_seq):
        adapted = self._adapt(sql, [])
        cur = self._conn.cursor()
        cur.executemany(adapted, params_seq)
        self._last_id = cur.lastrowid or self._last_id
        return _Cursor(cur, self._last_id)

    def executescript(self, script: str):
        """Execute a multi-statement script (splits on semicolons)."""
        cur = self._conn.cursor()
        for stmt in re.split(r";\s*\n", script):
            stmt = stmt.strip().rstrip(";")
            if stmt and not stmt.startswith("--"):
                try:
                    cur.execute(stmt)
                except Exception:
                    pass
        self._conn.commit()

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def get_connection() -> MySQLCompat:
    if pymysql is None:
        raise RuntimeError("PyMySQL is not installed. Run: pip install PyMySQL")
    conn = pymysql.connect(
        host=config.MYSQL_HOST,
        port=config.MYSQL_PORT,
        user=config.MYSQL_USER,
        password=config.MYSQL_PASS,
        database=config.MYSQL_DB,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
        charset="utf8mb4",
        connect_timeout=10,
    )
    return MySQLCompat(conn)
