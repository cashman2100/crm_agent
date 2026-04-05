"""SQLite interface for CRMArenaPro B2B database."""

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "data" / "crmarenapro_b2b_data.db"

# Safety: only allow read-only SELECT queries
ALLOWED_PREFIXES = ("SELECT", "WITH", "PRAGMA")


class CRMDatabase:
    def __init__(self, db_path: Path = DB_PATH):
        if not db_path.exists():
            raise FileNotFoundError(f"Database not found: {db_path}")
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    @staticmethod
    def _fix_reserved_words(sql: str) -> str:
        """Quote reserved SQL words used as table names."""
        import re
        # Only replace 'Case' as table name — after FROM/JOIN keywords
        sql = re.sub(
            r'\b(FROM|JOIN|INNER JOIN|LEFT JOIN|RIGHT JOIN|CROSS JOIN)\s+Case\b',
            lambda m: m.group(1) + ' "Case"',
            sql,
            flags=re.IGNORECASE,
        )
        return sql

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        """Execute a SELECT query and return list of dicts."""
        sql_upper = sql.strip().upper()
        if not any(sql_upper.startswith(p) for p in ALLOWED_PREFIXES):
            raise ValueError(f"Only SELECT queries allowed, got: {sql[:50]}")
        sql = self._fix_reserved_words(sql)
        try:
            cursor = self._conn.execute(sql, params)
            rows = cursor.fetchmany(50)  # limit to 50 rows
            return [dict(row) for row in rows]
        except sqlite3.Error as e:
            logger.error(f"SQL error: {e}\nQuery: {sql[:200]}")
            return []

    def tables(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        return [r[0] for r in rows]

    def schema(self, table: str) -> str:
        """Return column info for a table as text."""
        try:
            cols = self._conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            lines = [f"  {c[1]} ({c[2]})" for c in cols]
            count = self._conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            return f"Table {table} ({count} rows):\n" + "\n".join(lines)
        except sqlite3.Error:
            return f"Table {table}: not found"

    def close(self):
        self._conn.close()


# Singleton instance — created lazily
_db: CRMDatabase | None = None


def get_db() -> CRMDatabase | None:
    global _db
    if _db is None:
        try:
            _db = CRMDatabase()
            logger.info(f"Database connected: {DB_PATH}")
        except FileNotFoundError:
            logger.warning("CRM database not found — running without DB access")
    return _db
