"""
scripts/load_data.py
---------------------
Idempotent CSV → SQLite loader.

Run:
    python scripts/load_data.py [--csv path/to/sales.csv] [--db path/to/sales.db]

Design decisions:
- Idempotent: wipes sales + rejected_rows before every run so re-running
  after fixing the CSV doesn't create duplicate rows.
- Validation rules (rows that fail go to rejected_rows):
    * Order_date must parse as DD-MM-YYYY; stored as ISO YYYY-MM-DD.
    * Quantity must be an integer > 0.
    * Unit_price must be present and >= 0.
    * Discount_amount must be >= 0.
    * Revenue (qty * price - discount) must be >= 0.
- Returned transactions pass validation and go to `sales`; they are excluded
  from revenue calculations by the `valid_sales` VIEW, not here.
- A load summary is printed at the end.
"""

import csv
import json
import sys
import argparse
import pathlib
from datetime import datetime

# Allow running from project root without installing the package.
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from app.db import get_connection, init_db
from app import DB_PATH, PROJECT_ROOT


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def parse_date(raw: str) -> str | None:
    """Parse DD-MM-YYYY and return ISO YYYY-MM-DD, or None on failure."""
    try:
        return datetime.strptime(raw.strip(), "%d-%m-%Y").strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return None


def validate_row(row: dict) -> tuple[dict | None, str | None]:
    """
    Validate a CSV row dict.

    Returns:
        (clean_dict, None)  if valid
        (None, reason_str)  if invalid
    """
    txn_id = row.get("Transaction_Id", "").strip()

    # --- Date ---
    order_date = parse_date(row.get("Order_date", ""))
    if order_date is None:
        return None, f"Invalid or missing Order_date: {row.get('Order_date')!r}"

    # --- Quantity ---
    try:
        quantity = int(row.get("Quantity", "").strip())
    except (ValueError, AttributeError):
        return None, f"Non-integer Quantity: {row.get('Quantity')!r}"
    if quantity <= 0:
        return None, f"Quantity must be > 0; got {quantity}"

    # --- Unit price ---
    raw_price = row.get("Unit_price", "").strip()
    if raw_price == "":
        return None, "Missing Unit_price"
    try:
        unit_price = float(raw_price)
    except ValueError:
        return None, f"Non-numeric Unit_price: {raw_price!r}"
    if unit_price < 0:
        return None, f"Negative Unit_price: {unit_price}"

    # --- Discount ---
    raw_discount = row.get("Discount_amount", "0").strip() or "0"
    try:
        discount_amount = float(raw_discount)
    except ValueError:
        return None, f"Non-numeric Discount_amount: {raw_discount!r}"
    if discount_amount < 0:
        return None, f"Negative Discount_amount: {discount_amount}"

    # --- Revenue guard ---
    # Discount is a line-level total in currency units (not a percentage).
    revenue = quantity * unit_price - discount_amount
    if revenue < 0:
        return None, f"Negative revenue ({revenue}) after applying discount"

    return {
        "transaction_id":  txn_id,
        "order_date":      order_date,
        "product":         row.get("Product", "").strip(),
        "category":        row.get("Category", "").strip(),
        "region":          row.get("Region", "").strip(),
        "sales_channel":   row.get("Sales_channel", "").strip(),
        "quantity":        quantity,
        "unit_price":      unit_price,
        "discount_amount": discount_amount,
        "status":          row.get("Status", "").strip(),
        "revenue":         revenue,
    }, None


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load_csv(csv_path: pathlib.Path) -> None:
    """Load CSV into SQLite; print a summary afterwards."""

    # Ensure schema exists
    init_db()

    with get_connection() as conn:
        # --- Idempotent: clear existing data before re-loading ---
        conn.execute("DELETE FROM rejected_rows")
        conn.execute("DELETE FROM sales")

        good_rows: list[dict] = []
        bad_rows: list[tuple[str, str, str]] = []  # (txn_id, raw_json, reason)
        returned_rows: list[str] = []

        with open(csv_path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for raw_row in reader:
                clean, reason = validate_row(raw_row)

                if reason:
                    # Failed validation → rejected_rows
                    bad_rows.append((
                        raw_row.get("Transaction_Id", "UNKNOWN"),
                        json.dumps(raw_row),
                        reason,
                    ))
                else:
                    good_rows.append(clean)
                    if clean["status"] == "Returned":
                        returned_rows.append(clean["transaction_id"])

        # Bulk insert accepted rows
        conn.executemany(
            """INSERT OR REPLACE INTO sales
               (transaction_id, order_date, product, category, region,
                sales_channel, quantity, unit_price, discount_amount,
                status, revenue)
               VALUES (:transaction_id, :order_date, :product, :category,
                       :region, :sales_channel, :quantity, :unit_price,
                       :discount_amount, :status, :revenue)""",
            good_rows,
        )

        # Bulk insert rejected rows
        conn.executemany(
            "INSERT INTO rejected_rows (transaction_id, raw_data, reason) VALUES (?,?,?)",
            bad_rows,
        )

    # --- Print load summary ---
    print("\n" + "=" * 55)
    print("  DATA LOAD SUMMARY")
    print("=" * 55)
    print(f"  CSV file        : {csv_path}")
    print(f"  Rows loaded     : {len(good_rows)}")
    print(f"  Rows rejected   : {len(bad_rows)}")
    print(f"  Rows returned   : {len(returned_rows)}  (stored, excluded from revenue)")
    print("-" * 55)

    if bad_rows:
        print("  REJECTED DETAILS:")
        for txn_id, _, reason in bad_rows:
            print(f"    [{txn_id}] {reason}")

    if returned_rows:
        print("  RETURNED TRANSACTIONS (in sales, not in valid_sales view):")
        for txn_id in returned_rows:
            print(f"    [{txn_id}]")

    print("=" * 55 + "\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Load sales CSV into SQLite.")
    parser.add_argument(
        "--csv",
        default=str(PROJECT_ROOT / "data" / "sales.csv"),
        help="Path to the sales CSV file",
    )
    args = parser.parse_args()
    load_csv(pathlib.Path(args.csv))
