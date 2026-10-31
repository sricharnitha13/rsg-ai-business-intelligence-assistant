"""
app/main.py
------------
FastAPI application: routes, middleware, validation, and structured logging.

Endpoints:
  POST /ask              — main Q&A endpoint
  GET  /health           — liveness check
  GET  /data-summary     — rows loaded / rejected / excluded
"""

import logging
import logging.config
import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from app import PROJECT_ROOT
from app.ai import ask as ai_ask
from app.db import get_connection, init_db

# ---------------------------------------------------------------------------
# Structured logging setup
# ---------------------------------------------------------------------------
# Using basicConfig with JSON-friendly format for easy log parsing.

logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":%(message)s}',
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan: ensure DB schema exists on startup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: create tables/views if not already there.
    # The actual data is loaded separately by scripts/load_data.py.
    init_db()
    logger.info('"Server started, DB schema initialised"')
    yield
    logger.info('"Server shutting down"')


# ---------------------------------------------------------------------------
# App instance
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Mini AI BI Assistant",
    description="Ask natural-language questions about sales data.",
    version="1.0.0",
    lifespan=lifespan,
)

# Serve the single-page UI from /static
app.mount("/static", StaticFiles(directory=str(PROJECT_ROOT / "static")), name="static")


# ---------------------------------------------------------------------------
# Global exception handler — always returns JSON, never raw HTML
# ---------------------------------------------------------------------------

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error('"Unhandled exception: %s"', exc)
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "detail": str(exc)},
    )


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=500)

    @field_validator("question")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("question must not be blank")
        return v.strip()


class AskResponse(BaseModel):
    question:  str
    answer:    str
    intent:    str
    sql:       str
    columns:   list
    rows:      list
    path_used: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
async def root():
    """Serve the single-page UI."""
    return FileResponse(str(PROJECT_ROOT / "static" / "index.html"))


@app.get("/health", tags=["Meta"])
async def health():
    """Liveness check. Returns 200 with status ok if the server is running."""
    return {"status": "ok"}


@app.get("/data-summary", tags=["Meta"])
async def data_summary():
    """
    Return counts of loaded, rejected, and excluded (Returned) rows.
    Useful for verifying the data load before running queries.
    """
    try:
        with get_connection() as conn:
            loaded  = conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
            rejected = conn.execute("SELECT COUNT(*) FROM rejected_rows").fetchone()[0]
            returned = conn.execute(
                "SELECT COUNT(*) FROM sales WHERE status = 'Returned'"
            ).fetchone()[0]
            valid = conn.execute("SELECT COUNT(*) FROM valid_sales").fetchone()[0]

        return {
            "rows_in_sales_table":    loaded,
            "rows_rejected":          rejected,
            "rows_returned":          returned,
            "rows_in_valid_sales":    valid,
        }
    except sqlite3.OperationalError as exc:
        # Happens if load_data.py hasn't been run yet
        raise HTTPException(
            status_code=503,
            detail=f"Database not ready: {exc}. Run: python scripts/load_data.py",
        )


@app.post("/ask", response_model=AskResponse, tags=["Q&A"])
async def ask_endpoint(body: AskRequest):
    """
    Accept a natural-language question and return a structured answer.

    The AI layer classifies the intent, calls the matching analytics function,
    and phrases a sentence from the real SQL result.
    """
    logger.info('"question received: %s"', body.question)

    try:
        result = ai_ask(body.question)
    except Exception as exc:
        logger.error('"ai_ask failed: %s"', exc)
        raise HTTPException(status_code=500, detail=str(exc))

    logger.info('"answered: intent=%s path=%s"', result["intent"], result["path_used"])
    return result
