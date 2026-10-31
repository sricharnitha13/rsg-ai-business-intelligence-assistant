"""
app/analytics.py
-----------------
Five deterministic analytics functions backed by parameterised SQL.

Design decisions:
- NO LLM is involved here. All maths is done by SQLite.
- Every function returns {"columns": [...], "rows": [...], "sql": "..."}
  so the API layer can pass the raw SQL to the UI for transparency.
- All queries run against the `valid_sales` VIEW (Status='Completed') so
  Returned transactions are automatically excluded.
- revenue = quantity * unit_price - discount_amount is stored at load time;
  we SUM it directly rather than re-deriving it in SQL.
"""

from typing import Any
import sqlite3

from app.db import get_connection


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _run(sql: str, params: tuple = ()) -> dict[str, Any]:
    """Execute a SELECT and return {"columns", "rows", "sql"}."""
    with get_connection() as conn:
        cur = conn.execute(sql, params)
        columns = [d[0] for d in cur.description]
        rows = [list(r) for r in cur.fetchall()]
    return {"columns": columns, "rows": rows, "sql": sql.strip()}


# ---------------------------------------------------------------------------
# Analytics functions
# ---------------------------------------------------------------------------

def total_revenue() -> dict[str, Any]:
    """
    Return the grand total revenue across all valid (Completed) sales.
    Single-row result: [total_revenue].
    """
    sql = """
        SELECT ROUND(SUM(revenue), 2) AS total_revenue
        FROM   valid_sales
    """
    return _run(sql)


def top_product_by_revenue() -> dict[str, Any]:
    """
    Rank every product by total revenue (highest first).
    Returns full ranking so the caller can see context, not just the winner.
    """
    sql = """
        SELECT
            product,
            ROUND(SUM(revenue), 2)   AS total_revenue,
            SUM(quantity)            AS total_units_sold,
            RANK() OVER (ORDER BY SUM(revenue) DESC) AS rank
        FROM   valid_sales
        GROUP  BY product
        ORDER  BY total_revenue DESC
    """
    return _run(sql)


def monthly_revenue_trend() -> dict[str, Any]:
    """
    Aggregate revenue by calendar month (YYYY-MM).
    Useful for spotting seasonal patterns.
    """
    sql = """
        SELECT
            strftime('%Y-%m', order_date)  AS month,
            ROUND(SUM(revenue), 2)         AS total_revenue,
            COUNT(*)                       AS num_transactions
        FROM   valid_sales
        GROUP  BY month
        ORDER  BY month
    """
    return _run(sql)


def top_region_by_revenue() -> dict[str, Any]:
    """
    Rank every region by total revenue (highest first).
    Full ranking returned for context.
    """
    sql = """
        SELECT
            region,
            ROUND(SUM(revenue), 2)  AS total_revenue,
            COUNT(*)                AS num_transactions,
            RANK() OVER (ORDER BY SUM(revenue) DESC) AS rank
        FROM   valid_sales
        GROUP  BY region
        ORDER  BY total_revenue DESC
    """
    return _run(sql)


def revenue_change_between_months(month_a: str, month_b: str) -> dict[str, Any]:
    """
    Compare total revenue between two months (YYYY-MM format).

    Returns both totals, the absolute change, and the percentage change.

    Edge cases:
    - If month_a has no data, revenue_a is 0.
    - If month_a revenue is 0 (division by zero), pct_change is NULL / None.
    - The function does NOT assume month_a comes before month_b.

    Args:
        month_a: First month as 'YYYY-MM'
        month_b: Second month as 'YYYY-MM'
    """
    # Design: we use a single query with conditional aggregation so the
    # result is one row and easy to parse without two round-trips.
    sql = """
        SELECT
            ? AS month_a,
            ? AS month_b,
            ROUND(SUM(CASE WHEN strftime('%Y-%m', order_date) = ? THEN revenue ELSE 0 END), 2)
                AS revenue_a,
            ROUND(SUM(CASE WHEN strftime('%Y-%m', order_date) = ? THEN revenue ELSE 0 END), 2)
                AS revenue_b,
            ROUND(
                SUM(CASE WHEN strftime('%Y-%m', order_date) = ? THEN revenue ELSE 0 END) -
                SUM(CASE WHEN strftime('%Y-%m', order_date) = ? THEN revenue ELSE 0 END),
            2)  AS absolute_change,
            CASE
                WHEN SUM(CASE WHEN strftime('%Y-%m', order_date) = ? THEN revenue ELSE 0 END) = 0
                THEN NULL   -- division-by-zero guard; caller interprets NULL as "undefined"
                ELSE ROUND(
                    100.0 * (
                        SUM(CASE WHEN strftime('%Y-%m', order_date) = ? THEN revenue ELSE 0 END) -
                        SUM(CASE WHEN strftime('%Y-%m', order_date) = ? THEN revenue ELSE 0 END)
                    ) /
                    SUM(CASE WHEN strftime('%Y-%m', order_date) = ? THEN revenue ELSE 0 END),
                2)
            END AS pct_change
        FROM valid_sales
    """
    # Parameter order matches each ? placeholder above.
    params = (
        month_a, month_b,   # display labels
        month_a,             # revenue_a
        month_b,             # revenue_b
        month_b, month_a,    # absolute_change = b - a
        month_a,             # denominator for pct_change guard
        month_b, month_a,    # numerator for pct_change = b - a
        month_a,             # denominator for pct_change
    )
    return _run(sql, params)
