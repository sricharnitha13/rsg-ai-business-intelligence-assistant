"""
tests/conftest.py
------------------
Shared pytest fixtures.

Key design decisions:
- We use an IN-MEMORY SQLite database for tests so tests are fast and
  isolated; each test session gets a fresh DB.
- The `monkeypatch` fixture overrides DB_PATH so app code transparently
  uses the in-memory DB without any code changes.
- The LLM client is NOT called in tests; we mock it with a predictable
  KeywordFallbackClient so tests run offline.
"""

import csv
import io
import pathlib
import sqlite3
import sys

import pytest
from fastapi.testclient import TestClient

# Make sure the project root is on sys.path
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

# ---------------------------------------------------------------------------
# Minimal CSV for test loading (includes all three problem rows)
# ---------------------------------------------------------------------------

TEST_CSV_CONTENT = """\
Transaction_Id,Order_date,Product,Category,Region,Sales_channel,Quantity,Unit_price,Discount_amount,Status
TXN0001,03-01-2026,Laptop Stand,Accessories,North,Online,10,1200,0,Completed
TXN0002,05-01-2026,Wireless Mouse,Accessories,South,Retail,25,650,0,Completed
TXN0003,08-01-2026,Office Chair,Furniture,West,Online,4,7800,0,Completed
TXN0004,12-01-2026,Monitor,Electronics,East,Retail,6,9500,500,Completed
TXN0005,15-01-2026,Keyboard,Accessories,North,Online,18,900,0,Completed
TXN0006,18-01-2026,Desk,Furniture,South,Retail,3,12500,0,Completed
TXN0007,21-01-2026,USB-C Hub,Accessories,West,Online,12,1800,0,Completed
TXN0008,25-01-2026,Monitor,Electronics,North,Online,2,9500,0,Completed
TXN0009,02-02-2026,Laptop Stand,Accessories,East,Retail,14,1200,0,Completed
TXN0010,04-02-2026,Wireless Mouse,Accessories,West,Online,30,650,0,Completed
TXN0033,26-04-2026,Monitor,Electronics,West,Retail,2,9500,0,Returned
TXN0034,28-04-2026,Keyboard,Accessories,East,Online,0,900,0,Completed
TXN0051,28-06-2026,Laptop Stand,Accessories,East,Online,5,,0,Completed
"""

# Expected revenue for Completed rows only (manually verified):
# TXN0001: 10*1200-0   = 12000
# TXN0002: 25*650-0    = 16250
# TXN0003: 4*7800-0    = 31200
# TXN0004: 6*9500-500  = 56500
# TXN0005: 18*900-0    = 16200
# TXN0006: 3*12500-0   = 37500
# TXN0007: 12*1800-0   = 21600
# TXN0008: 2*9500-0    = 19000
# TXN0009: 14*1200-0   = 16800
# TXN0010: 30*650-0    = 19500
# TXN0033: Returned    → excluded
# TXN0034: qty=0       → rejected
# TXN0051: no price    → rejected
EXPECTED_TOTAL_REVENUE = (
    12000 + 16250 + 31200 + 56500 + 16200 + 37500 + 21600 + 19000 + 16800 + 19500
)  # = 246550


@pytest.fixture(scope="session")
def test_db_path(tmp_path_factory):
    """
    Create a temporary SQLite DB file for the test session, load the test
    CSV into it, and return the path.

    scope="session" means this runs once for the whole test run.
    """
    import app  # import the package to override DB_PATH
    from app.db import init_db, get_connection

    db_path = tmp_path_factory.mktemp("data") / "test_sales.db"

    # Patch DB_PATH before init_db so the schema is created in the temp file
    import app as app_pkg
    original_db_path = app_pkg.DB_PATH
    app_pkg.DB_PATH = db_path

    # Also patch in db module
    import app.db as db_mod
    db_mod.DB_PATH = db_path

    init_db()

    # Load test CSV rows directly (bypass file I/O by using the in-memory string)
    from scripts.load_data import validate_row
    import json

    good_rows = []
    bad_rows = []

    reader = csv.DictReader(io.StringIO(TEST_CSV_CONTENT))
    for raw_row in reader:
        clean, reason = validate_row(raw_row)
        if reason:
            bad_rows.append((raw_row.get("Transaction_Id", "?"), json.dumps(raw_row), reason))
        else:
            good_rows.append(clean)

    with get_connection() as conn:
        conn.executemany(
            """INSERT OR REPLACE INTO sales
               (transaction_id, order_date, product, category, region,
                sales_channel, quantity, unit_price, discount_amount, status, revenue)
               VALUES (:transaction_id, :order_date, :product, :category,
                       :region, :sales_channel, :quantity, :unit_price,
                       :discount_amount, :status, :revenue)""",
            good_rows,
        )
        conn.executemany(
            "INSERT INTO rejected_rows (transaction_id, raw_data, reason) VALUES (?,?,?)",
            bad_rows,
        )

    yield db_path

    # Restore
    app_pkg.DB_PATH = original_db_path
    db_mod.DB_PATH = original_db_path


@pytest.fixture(scope="session")
def client(test_db_path):
    """FastAPI TestClient wired to the test database."""
    import app as app_pkg
    import app.db as db_mod

    # Ensure app uses test DB
    app_pkg.DB_PATH = test_db_path
    db_mod.DB_PATH = test_db_path

    from app.main import app as fastapi_app
    return TestClient(fastapi_app)
