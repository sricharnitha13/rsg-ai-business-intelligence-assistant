"""
app/__init__.py
Package marker. Also exposes DB_PATH so other modules get a single source of truth.
"""
import pathlib

# Project root is two levels up from this file (app/ -> project root)
PROJECT_ROOT = pathlib.Path(__file__).parent.parent
DB_PATH = PROJECT_ROOT / "data" / "sales.db"
