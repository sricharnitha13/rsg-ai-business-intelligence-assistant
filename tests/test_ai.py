"""
tests/test_ai.py
-----------------
Tests for the AI layer: intent routing, SQL validator, and fallback client.
The LLM is NOT called; we test only the keyword fallback and validator logic.
"""

import pytest
from app.ai import (
    validate_sql,
    KeywordFallbackClient,
    _verify_answer_numbers,
    _template_answer,
    SUPPORTED_INTENTS,
)


# ---------------------------------------------------------------------------
# KeywordFallbackClient — intent routing
# ---------------------------------------------------------------------------

client = KeywordFallbackClient()


def test_fallback_total_revenue():
    resp = client.get_intent("What is the total revenue?")
    assert resp.intent == "total_revenue"


def test_fallback_top_product():
    resp = client.get_intent("Which product generated the highest revenue?")
    assert resp.intent == "top_product_by_revenue"


def test_fallback_monthly_trend():
    resp = client.get_intent("Show me the monthly revenue trend")
    assert resp.intent == "monthly_revenue_trend"


def test_fallback_top_region():
    resp = client.get_intent("Which region had the highest sales?")
    assert resp.intent == "top_region_by_revenue"


def test_fallback_revenue_change():
    resp = client.get_intent("How did revenue change between 2026-01 and 2026-02?")
    assert resp.intent == "revenue_change_between_months"
    assert resp.params["month_a"] == "2026-01"
    assert resp.params["month_b"] == "2026-02"


def test_fallback_unsupported():
    resp = client.get_intent("What is the weather in Paris?")
    assert resp.intent == "unsupported"


# ---------------------------------------------------------------------------
# SQL validator
# ---------------------------------------------------------------------------

def test_sql_validator_allows_valid_select():
    ok, result = validate_sql("SELECT * FROM valid_sales LIMIT 5")
    assert ok is True


def test_sql_validator_blocks_drop():
    ok, reason = validate_sql("DROP TABLE sales")
    assert ok is False
    assert "drop" in reason.lower()


def test_sql_validator_blocks_delete():
    ok, reason = validate_sql("DELETE FROM sales WHERE 1=1")
    assert ok is False
    assert "delete" in reason.lower()


def test_sql_validator_blocks_multiple_statements():
    ok, reason = validate_sql("SELECT 1; DROP TABLE sales")
    assert ok is False


def test_sql_validator_blocks_non_whitelisted_table():
    ok, reason = validate_sql("SELECT * FROM sales")   # `sales` not in ALLOWED_TABLES
    assert ok is False
    assert "sales" in reason.lower()


def test_sql_validator_injects_limit():
    """If LIMIT is missing, the validator should add one."""
    ok, sql_out = validate_sql("SELECT product FROM valid_sales")
    assert ok is True
    assert "LIMIT" in sql_out.upper()


# ---------------------------------------------------------------------------
# Answer number-check
# ---------------------------------------------------------------------------

def test_number_check_passes_when_all_numbers_present():
    result = {"columns": ["total_revenue"], "rows": [[246550.0]]}
    assert _verify_answer_numbers("Total revenue is 246550.0.", result) is True


def test_number_check_fails_when_hallucinated():
    result = {"columns": ["total_revenue"], "rows": [[246550.0]]}
    # 999999 does not appear in the result
    assert _verify_answer_numbers("Total revenue is 999999.", result) is False


def test_template_answer_no_data():
    result = {"columns": ["total_revenue"], "rows": []}
    assert "No data" in _template_answer(result)
