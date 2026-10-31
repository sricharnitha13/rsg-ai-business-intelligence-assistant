"""
app/db.py
----------
Database connection helpers and schema definitions.

Design decisions:
- We use stdlib sqlite3 only (no SQLAlchemy) to keep the dependency list short
  and make the code easy to explain in an interview.
- Revenue is computed and stored as a real column at INSERT time rather than
  using a SQLite generated column, which avoids syntax differences between
  SQLite versions and makes SELECT * naturally include revenue.
- Discount_amount is treated as a line-level TOTAL discount in currency units
  (not a percentage). This is the documented assumption.
- The `valid_sales` VIEW excludes rows where Status != 'Completed'. Returned
  transactions are kept in the `sales` table for audit purposes but never
  counted in any revenue calculation.
- For the guarded text-to-SQL path we open a second, READ-ONLY connection
  using the URI mode (file:path?mode=ro) so SQLite itself enforces the
  read-only constraint as a second line of defence.
"""

import sqlite3
from app import DB_PATH


# ---------------------------------------------------------------------------
# DDL: table + view definitions
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
-- Main fact table for all accepted transactions (including Returned).
CREATE TABLE IF NOT EXISTS sales (
    transaction_id  TEXT PRIMARY KEY,
    order_date      TEXT NOT NULL,   -- ISO YYYY-MM-DD
    product         TEXT NOT NULL,
    category        TEXT NOT NULL,
    region          TEXT NOT NULL,
    sales_channel   TEXT NOT NULL,
    quantity        INTEGER NOT NULL,
    unit_price      REAL NOT NULL,
    discount_amount REAL NOT NULL DEFAULT 0,
    status          TEXT NOT NULL,
    -- Revenue is quantity * unit_price - discount_amount (line-level total discount).
    -- Stored at insert time so queries don't need to repeat the expression.
    revenue         REAL NOT NULL
);

-- Rows that failed validation go here with a human-readable reason.
CREATE TABLE IF NOT EXISTS rejected_rows (
    transaction_id  TEXT,
    raw_data        TEXT,   -- JSON-encoded original CSV row for debugging
    reason          TEXT NOT NULL
);

-- View: only Completed transactions contribute to reported revenue.
-- Returned rows are stored in `sales` for audit but excluded here.
CREATE VIEW IF NOT EXISTS valid_sales AS
    SELECT * FROM sales WHERE status = 'Completed';
"""


def get_connection(read_only: bool = False) -> sqlite3.Connection:
    """
    Return a sqlite3 connection to the project database.

    Args:
        read_only: If True, open in URI read-only mode. Used by the
                   guarded text-to-SQL path to prevent accidental writes.
    """
    db_path = str(DB_PATH)

    if read_only:
        # URI mode: mode=ro makes SQLite reject any write attempt at the
        # engine level, which is a second line of defence after our SQL
        # validator. Forward slashes are required in the URI even on Windows.
        uri = f"file:{db_path.replace(chr(92), '/')}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        conn = sqlite3.connect(db_path)

    # Return rows as dict-like objects so callers can use row["column_name"].
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create tables and views if they don't already exist."""
    with get_connection() as conn:
        conn.executescript(SCHEMA_SQL)
