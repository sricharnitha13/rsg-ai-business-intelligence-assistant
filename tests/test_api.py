"""
tests/test_api.py
------------------
Integration tests for the FastAPI endpoints using TestClient.
The AI layer uses KeywordFallbackClient (no real LLM) because GEMINI_API_KEY
is not set in the test environment.
"""

import pytest


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# /data-summary
# ---------------------------------------------------------------------------

def test_data_summary(client):
    resp = client.get("/data-summary")
    assert resp.status_code == 200
    data = resp.json()
    assert "rows_in_sales_table" in data
    assert "rows_rejected" in data
    # We know 2 rows are rejected in the test dataset (TXN0034, TXN0051)
    assert data["rows_rejected"] == 2


# ---------------------------------------------------------------------------
# /ask — valid questions
# ---------------------------------------------------------------------------

def test_ask_total_revenue(client):
    resp = client.post("/ask", json={"question": "What is the total revenue?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["intent"] == "total_revenue"
    assert len(body["rows"]) > 0
    assert body["sql"] != ""


def test_ask_top_product(client):
    resp = client.post("/ask", json={"question": "Which product generated the highest revenue?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["intent"] == "top_product_by_revenue"


def test_ask_monthly_trend(client):
    resp = client.post("/ask", json={"question": "Show monthly revenue trend"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["intent"] == "monthly_revenue_trend"


def test_ask_top_region(client):
    resp = client.post("/ask", json={"question": "Which region had the highest sales?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["intent"] == "top_region_by_revenue"


def test_ask_revenue_change(client):
    resp = client.post(
        "/ask",
        json={"question": "How did revenue change between 2026-01 and 2026-02?"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["intent"] == "revenue_change_between_months"
    assert len(body["rows"]) == 1


# ---------------------------------------------------------------------------
# /ask — invalid / edge-case inputs
# ---------------------------------------------------------------------------

def test_ask_empty_question(client):
    """Empty question should return 422 (Pydantic validation error)."""
    resp = client.post("/ask", json={"question": ""})
    assert resp.status_code == 422


def test_ask_oversized_question(client):
    """Question exceeding 500 chars should return 422."""
    resp = client.post("/ask", json={"question": "x" * 501})
    assert resp.status_code == 422


def test_ask_blank_whitespace_question(client):
    """All-whitespace question should return 422."""
    resp = client.post("/ask", json={"question": "   "})
    assert resp.status_code == 422


def test_ask_unsupported_question(client):
    """Question about unrelated topic should return intent='unsupported'."""
    resp = client.post("/ask", json={"question": "What is the capital of France?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["intent"] == "unsupported"


def test_ask_path_used_field_present(client):
    """path_used field must always be present in the response."""
    resp = client.post("/ask", json={"question": "What is total revenue?"})
    assert resp.status_code == 200
    assert "path_used" in resp.json()


def test_ask_month_no_data(client):
    """Asking about a month with no data should still return a valid response."""
    resp = client.post(
        "/ask",
        json={"question": "How did revenue change between 2026-01 and 2099-12?"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["intent"] == "revenue_change_between_months"
    # revenue_b (2099-12) should be 0
    row = dict(zip(body["columns"], body["rows"][0]))
    assert row["revenue_b"] == 0.0
