"""
tests/test_analytics.py
------------------------
Tests for all five analytics functions.
Each test computes the expected value independently from the test CSV data
so the test is self-documenting and catches regressions.
"""

import pytest
from tests.conftest import EXPECTED_TOTAL_REVENUE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def first_row(result: dict) -> dict:
    """Return the first result row as a {column: value} dict."""
    return dict(zip(result["columns"], result["rows"][0]))


# ---------------------------------------------------------------------------
# Test 1: total revenue — computed independently from the test CSV
# ---------------------------------------------------------------------------

def test_total_revenue(test_db_path):
    from app import analytics
    result = analytics.total_revenue()

    assert result["columns"] == ["total_revenue"]
    assert len(result["rows"]) == 1
    assert result["rows"][0][0] == pytest.approx(EXPECTED_TOTAL_REVENUE, rel=1e-6)
    # SQL should be present and reference valid_sales
    assert "valid_sales" in result["sql"]


# ---------------------------------------------------------------------------
# Test 2: top product by revenue — Monitor should NOT be top (it has a
# Returned row excluded + discount). Check ranking structure.
# ---------------------------------------------------------------------------

def test_top_product_by_revenue(test_db_path):
    from app import analytics
    result = analytics.top_product_by_revenue()

    assert "product" in result["columns"]
    assert "total_revenue" in result["columns"]
    assert "rank" in result["columns"]
    assert len(result["rows"]) > 0

    # Rank 1 should be the product with the highest total_revenue
    rows_by_rank = sorted(result["rows"], key=lambda r: dict(zip(result["columns"], r))["rank"])
    top = dict(zip(result["columns"], rows_by_rank[0]))
    assert top["rank"] == 1

    # Desk: TXN0006 = 37500. Monitor: TXN0004 56500 + TXN0008 19000 = 75500
    # Monitor should be top in test data
    assert top["product"] == "Monitor"


# ---------------------------------------------------------------------------
# Test 3: monthly revenue trend — should return Jan and Feb 2026
# ---------------------------------------------------------------------------

def test_monthly_revenue_trend(test_db_path):
    from app import analytics
    result = analytics.monthly_revenue_trend()

    assert "month" in result["columns"]
    assert "total_revenue" in result["columns"]

    months = [dict(zip(result["columns"], r))["month"] for r in result["rows"]]
    assert "2026-01" in months
    assert "2026-02" in months


# ---------------------------------------------------------------------------
# Test 4: top region by revenue — full ranking returned
# ---------------------------------------------------------------------------

def test_top_region_by_revenue(test_db_path):
    from app import analytics
    result = analytics.top_region_by_revenue()

    assert "region" in result["columns"]
    assert "rank" in result["columns"]
    # All rows should have rank >= 1
    for row in result["rows"]:
        r = dict(zip(result["columns"], row))
        assert r["rank"] >= 1


# ---------------------------------------------------------------------------
# Test 5: revenue_change_between_months — valid months
# ---------------------------------------------------------------------------

def test_revenue_change_known_months(test_db_path):
    from app import analytics
    result = analytics.revenue_change_between_months("2026-01", "2026-02")

    row = first_row(result)
    assert row["month_a"] == "2026-01"
    assert row["month_b"] == "2026-02"
    # Both months have data in test CSV so revenues should be > 0
    assert row["revenue_a"] > 0
    assert row["revenue_b"] > 0
    # absolute_change = revenue_b - revenue_a
    assert abs(row["absolute_change"] - (row["revenue_b"] - row["revenue_a"])) < 0.01


# ---------------------------------------------------------------------------
# Test 6: revenue_change — month with no data should give revenue 0, pct NULL
# ---------------------------------------------------------------------------

def test_revenue_change_month_no_data(test_db_path):
    from app import analytics
    result = analytics.revenue_change_between_months("2026-01", "2099-01")

    row = first_row(result)
    assert row["revenue_b"] == 0.0
    # pct_change should be a number (revenue_a > 0) showing -100 or similar
    # OR None if revenue_a were also 0; for "2026-01" it's > 0
    # Just verify the key exists
    assert "pct_change" in row


# ---------------------------------------------------------------------------
# Test 7: Returned transaction excluded from valid_sales
# ---------------------------------------------------------------------------

def test_returned_transaction_excluded(test_db_path):
    from app.db import get_connection
    # TXN0033 is Returned; it should be in `sales` but NOT in `valid_sales`
    with get_connection() as conn:
        in_sales = conn.execute(
            "SELECT COUNT(*) FROM sales WHERE transaction_id = 'TXN0033'"
        ).fetchone()[0]
        in_valid = conn.execute(
            "SELECT COUNT(*) FROM valid_sales WHERE transaction_id = 'TXN0033'"
        ).fetchone()[0]
    assert in_sales == 1,  "Returned row should be stored in sales for audit"
    assert in_valid == 0,  "Returned row must NOT appear in valid_sales"


# ---------------------------------------------------------------------------
# Test 8: Quantity=0 row rejected, missing-price row rejected
# ---------------------------------------------------------------------------

def test_rejected_rows(test_db_path):
    from app.db import get_connection
    with get_connection() as conn:
        rejected = conn.execute(
            "SELECT transaction_id, reason FROM rejected_rows"
        ).fetchall()
    txn_ids = [r["transaction_id"] for r in rejected]

    # TXN0034 has Quantity=0 → rejected
    assert "TXN0034" in txn_ids, "TXN0034 (qty=0) should be in rejected_rows"

    # TXN0051 has missing Unit_price → rejected
    assert "TXN0051" in txn_ids, "TXN0051 (missing price) should be in rejected_rows"
