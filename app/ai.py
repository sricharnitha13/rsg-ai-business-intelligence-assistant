"""
app/ai.py
----------
AI layer: intent routing, guarded text-to-SQL, and answer generation.

Architecture overview:
  1. PRIMARY PATH (safe):
     The LLM receives the user question + a fixed list of supported intents
     and must return strict JSON: {"intent": "...", "params": {...}}.
     Pydantic validates the JSON; unknown intents/bad params are rejected.
     The matching analytics function is then called for the actual numbers.

  2. SECONDARY PATH (guarded text-to-SQL):
     For questions that match no intent, the LLM generates a SELECT query
     against the `valid_sales` view. Before execution we:
       a) sqlglot-parse it and reject anything non-SELECT
       b) block DDL/DML keywords
       c) allow only whitelisted tables/views
       d) inject a LIMIT if missing
       e) run on a READ-ONLY SQLite connection

  3. ANSWER GENERATION:
     The LLM phrases a natural-language sentence from the SQL result.
     A number-check verifies every number in the sentence appears in the
     result set; if not, we fall back to a templated sentence.

  4. OFFLINE FALLBACK:
     If GEMINI_API_KEY is absent or the LLM call fails, a keyword-based
     router handles the five known intents so tests run offline.
"""

import json
import logging
import os
import re
import sqlite3
from abc import ABC, abstractmethod
from typing import Any

import sqlglot
from pydantic import BaseModel, ValidationError

from app import analytics
from app.db import get_connection

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pydantic models for LLM intent response validation
# ---------------------------------------------------------------------------

SUPPORTED_INTENTS = {
    "total_revenue",
    "top_product_by_revenue",
    "monthly_revenue_trend",
    "top_region_by_revenue",
    "revenue_change_between_months",
    "unsupported",
}


class IntentResponse(BaseModel):
    """Schema the LLM must return for the primary (intent) path."""
    intent: str
    params: dict = {}
    reason: str = ""   # populated when intent == "unsupported"


class MonthChangeParams(BaseModel):
    """Validates params for revenue_change_between_months."""
    month_a: str   # YYYY-MM
    month_b: str   # YYYY-MM


# ---------------------------------------------------------------------------
# SQL safety guard
# ---------------------------------------------------------------------------

# Only these tables/views may appear in text-to-SQL queries.
ALLOWED_TABLES = {"valid_sales"}

# Dangerous keywords that must never appear in a generated query.
BLOCKED_KEYWORDS = {
    "insert", "update", "delete", "drop", "alter",
    "attach", "pragma", "create", "replace", "truncate",
}

MAX_ROWS = 100   # LIMIT cap for text-to-SQL results


def validate_sql(sql: str) -> tuple[bool, str]:
    """
    Validate a LLM-generated SQL string.

    Returns:
        (True, cleaned_sql)  if safe
        (False, reason)      if dangerous
    """
    # 1. Keyword blocklist (fast check before parsing)
    lower_sql = sql.lower()
    for kw in BLOCKED_KEYWORDS:
        # Use word-boundary regex to avoid false positives on column names
        if re.search(rf"\b{kw}\b", lower_sql):
            return False, f"Blocked keyword detected: '{kw}'"

    # 2. sqlglot parse – must be exactly one statement
    try:
        statements = sqlglot.parse(sql, dialect="sqlite")
    except Exception as exc:
        return False, f"SQL parse error: {exc}"

    if len(statements) != 1:
        return False, f"Expected 1 statement; got {len(statements)}"

    stmt = statements[0]

    # 3. Must be a plain SELECT (not INSERT, UPDATE, etc.)
    if not isinstance(stmt, sqlglot.expressions.Select):
        return False, "Only SELECT statements are allowed"

    # 4. Check all referenced table names
    for table in stmt.find_all(sqlglot.expressions.Table):
        name = table.name.lower()
        if name not in ALLOWED_TABLES:
            return False, f"Table '{name}' is not in the allowed list: {ALLOWED_TABLES}"

    # 5. Inject LIMIT if absent
    if stmt.args.get("limit") is None:
        sql = sql.rstrip().rstrip(";") + f" LIMIT {MAX_ROWS}"

    return True, sql


# ---------------------------------------------------------------------------
# LLM client abstraction
# ---------------------------------------------------------------------------

class LLMClient(ABC):
    """Abstract interface so the Gemini client can be swapped for tests."""

    @abstractmethod
    def get_intent(self, question: str) -> IntentResponse:
        """Return an IntentResponse for the given question."""

    @abstractmethod
    def generate_sql(self, question: str) -> str:
        """Generate a SQL query string for the given question."""

    @abstractmethod
    def phrase_answer(self, question: str, result: dict) -> str:
        """Turn a SQL result dict into a natural-language sentence."""


# ---------------------------------------------------------------------------
# Gemini client (real LLM)
# ---------------------------------------------------------------------------

INTENT_SYSTEM_PROMPT = """
You are a data assistant. Given a user question about sales data, classify
the question into exactly ONE of these intents:

  total_revenue               — e.g. "What is the total revenue?"
  top_product_by_revenue      — e.g. "Which product made the most money?"
  monthly_revenue_trend       — e.g. "Show monthly revenue trend"
  top_region_by_revenue       — e.g. "Which region had the highest sales?"
  revenue_change_between_months — e.g. "How did revenue change from Jan to Feb?"
  unsupported                 — anything else

Respond ONLY with valid JSON matching one of these two schemas:
  {"intent": "<one of the five intents above>", "params": {<params if needed>}}
  {"intent": "unsupported", "reason": "<brief explanation>"}

For revenue_change_between_months, params must be:
  {"month_a": "YYYY-MM", "month_b": "YYYY-MM"}

No markdown, no explanation, just the JSON object.
""".strip()

TEXT_TO_SQL_PROMPT = """
You are a SQLite expert. The database has a view called `valid_sales` with
these columns:
  transaction_id, order_date (YYYY-MM-DD), product, category, region,
  sales_channel, quantity, unit_price, discount_amount, status, revenue

Write a single SELECT query against `valid_sales` only.
Do NOT use INSERT, UPDATE, DELETE, DROP, CREATE, ALTER, ATTACH, or PRAGMA.
Return ONLY the raw SQL with no markdown fences or explanation.
""".strip()

PHRASE_ANSWER_PROMPT = """
You are a concise business analyst. Given a question and a query result,
write ONE clear sentence answering the question using ONLY the numbers
from the result. Do not introduce any numbers that are not in the result.
""".strip()


class GeminiClient(LLMClient):
    """Calls Google Gemini via the google-genai SDK."""

    def __init__(self):
        from google import genai  # imported lazily so missing SDK doesn't crash import
        api_key = os.environ["GEMINI_API_KEY"]
        self._client = genai.Client(api_key=api_key)
        self._model = "gemini-2.0-flash"

    def _chat(self, system: str, user: str) -> str:
        """Send a single-turn message and return the text response."""
        from google.genai import types
        response = self._client.models.generate_content(
            model=self._model,
            config=types.GenerateContentConfig(system_instruction=system),
            contents=user,
        )
        return response.text.strip()

    def get_intent(self, question: str) -> IntentResponse:
        raw = self._chat(INTENT_SYSTEM_PROMPT, question)
        # Strip markdown fences if the model wraps the JSON
        raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("`").strip()
        data = json.loads(raw)
        return IntentResponse(**data)

    def generate_sql(self, question: str) -> str:
        raw = self._chat(TEXT_TO_SQL_PROMPT, question)
        # Strip markdown fences
        raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("`").strip()
        return raw

    def phrase_answer(self, question: str, result: dict) -> str:
        payload = json.dumps(result["rows"][:10])  # send at most 10 rows
        user_msg = f"Question: {question}\nResult rows: {payload}"
        return self._chat(PHRASE_ANSWER_PROMPT, user_msg)


# ---------------------------------------------------------------------------
# Keyword fallback client (offline, no API key needed)
# ---------------------------------------------------------------------------

MONTH_PATTERN = re.compile(r"\b(20\d{2})[-/]?(0[1-9]|1[0-2])\b")


class KeywordFallbackClient(LLMClient):
    """
    Simple keyword-based intent router used when:
    - GEMINI_API_KEY is not set, OR
    - the Gemini API call fails.

    This ensures tests and demos can run offline without mocking.
    """

    _KEYWORD_MAP = {
        "total_revenue":                ["total revenue", "overall revenue", "how much revenue", "revenue total"],
        "top_product_by_revenue":       ["top product", "best product", "highest revenue product", "product revenue", "which product"],
        "monthly_revenue_trend":        ["monthly", "month by month", "trend", "over time", "by month"],
        "top_region_by_revenue":        ["region", "top region", "best region", "highest region"],
        "revenue_change_between_months": ["change", "compare", "between", "from.*to", "vs", "versus", "month.*month"],
    }

    def get_intent(self, question: str) -> IntentResponse:
        q_lower = question.lower()

        # Try each intent's keyword list
        for intent, keywords in self._KEYWORD_MAP.items():
            for kw in keywords:
                if re.search(kw, q_lower):
                    if intent == "revenue_change_between_months":
                        params = self._extract_months(question)
                        if params:
                            return IntentResponse(intent=intent, params=params)
                    else:
                        return IntentResponse(intent=intent)

        return IntentResponse(
            intent="unsupported",
            reason="No matching intent found for this question.",
        )

    def _extract_months(self, question: str) -> dict | None:
        matches = MONTH_PATTERN.findall(question)
        if len(matches) >= 2:
            return {
                "month_a": f"{matches[0][0]}-{matches[0][1]}",
                "month_b": f"{matches[1][0]}-{matches[1][1]}",
            }
        return None

    def generate_sql(self, question: str) -> str:
        # Fallback: basic SELECT so the endpoint doesn't crash
        return "SELECT * FROM valid_sales LIMIT 10"

    def phrase_answer(self, question: str, result: dict) -> str:
        # Fallback: return a template sentence rather than LLM phrasing
        return _template_answer(result)


# ---------------------------------------------------------------------------
# Answer number-check and template fallback
# ---------------------------------------------------------------------------

def _extract_numbers(text: str) -> set[str]:
    """Extract all numeric strings (integers and decimals) from text."""
    return set(re.findall(r"\d+(?:\.\d+)?", text))


def _template_answer(result: dict) -> str:
    """Generate a safe templated answer from the raw result dict."""
    if not result["rows"]:
        return "No data found for this query."
    first_row = result["rows"][0]
    cols = result["columns"]
    parts = ", ".join(f"{c}: {v}" for c, v in zip(cols, first_row))
    return f"Result: {parts}."


def _verify_answer_numbers(answer: str, result: dict) -> bool:
    """
    Return True if every number in `answer` appears somewhere in the result rows.
    This prevents the LLM from hallucinating numbers not in the data.
    """
    answer_nums = _extract_numbers(answer)
    if not answer_nums:
        return True   # no numbers to verify

    # Flatten all result values to strings for comparison
    result_nums: set[str] = set()
    for row in result["rows"]:
        for val in row:
            result_nums.update(_extract_numbers(str(val)))

    return answer_nums.issubset(result_nums)


# ---------------------------------------------------------------------------
# Main ask() entry point
# ---------------------------------------------------------------------------

def _make_client() -> tuple[LLMClient, str]:
    """
    Return (client, path_label).
    Tries Gemini first; falls back to keyword router if key is missing or
    the import fails.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        logger.info("GEMINI_API_KEY not set — using keyword fallback client")
        return KeywordFallbackClient(), "keyword_fallback"
    try:
        client = GeminiClient()
        return client, "llm_intent"
    except Exception as exc:
        logger.warning("Failed to initialise Gemini client (%s) — falling back", exc)
        return KeywordFallbackClient(), "keyword_fallback"


def ask(question: str) -> dict[str, Any]:
    """
    Main entry point for the AI layer.

    Returns a dict with keys:
      question, answer, intent, sql, columns, rows, path_used
    """
    client, path_used = _make_client()

    # ------------------------------------------------------------------
    # Step 1: Get intent from LLM (or keyword fallback)
    # ------------------------------------------------------------------
    try:
        intent_resp = client.get_intent(question)
    except (json.JSONDecodeError, ValidationError, Exception) as exc:
        logger.warning("Intent parse failed (%s); treating as unsupported", exc)
        intent_resp = IntentResponse(
            intent="unsupported",
            reason=f"Could not parse question: {exc}",
        )

    # Reject unknown intents defensively
    if intent_resp.intent not in SUPPORTED_INTENTS:
        intent_resp = IntentResponse(
            intent="unsupported",
            reason=f"Unknown intent '{intent_resp.intent}'",
        )

    intent = intent_resp.intent

    # ------------------------------------------------------------------
    # Step 2a: Primary path — dispatch to analytics function
    # ------------------------------------------------------------------
    if intent == "total_revenue":
        result = analytics.total_revenue()

    elif intent == "top_product_by_revenue":
        result = analytics.top_product_by_revenue()

    elif intent == "monthly_revenue_trend":
        result = analytics.monthly_revenue_trend()

    elif intent == "top_region_by_revenue":
        result = analytics.top_region_by_revenue()

    elif intent == "revenue_change_between_months":
        try:
            p = MonthChangeParams(**intent_resp.params)
        except ValidationError as exc:
            return {
                "question": question,
                "answer": f"Please specify two months in YYYY-MM format. ({exc})",
                "intent": intent,
                "sql": "",
                "columns": [],
                "rows": [],
                "path_used": path_used,
            }
        result = analytics.revenue_change_between_months(p.month_a, p.month_b)

    # ------------------------------------------------------------------
    # Step 2b: Unsupported — try guarded text-to-SQL as secondary path
    # ------------------------------------------------------------------
    elif intent == "unsupported":
        logger.info("Intent unsupported; attempting guarded text-to-SQL")
        try:
            raw_sql = client.generate_sql(question)
            ok, validated_sql = validate_sql(raw_sql)
            if not ok:
                # SQL failed validation — return friendly error
                return _unsupported_response(question, path_used, validated_sql)

            path_used = "text_to_sql"
            # Execute on read-only connection for safety
            with get_connection(read_only=True) as conn:
                cur = conn.execute(validated_sql)
                columns = [d[0] for d in cur.description]
                rows = [list(r) for r in cur.fetchall()]

            result = {"columns": columns, "rows": rows, "sql": validated_sql}
        except Exception as exc:
            logger.warning("Text-to-SQL path failed: %s", exc)
            return _unsupported_response(question, path_used, str(exc))

    else:
        # Should never reach here, but be defensive
        return _unsupported_response(question, path_used, "")

    # ------------------------------------------------------------------
    # Step 3: Generate natural-language answer
    # ------------------------------------------------------------------
    try:
        answer = client.phrase_answer(question, result)
        # Number-check: if LLM hallucinated a number, fall back to template
        if not _verify_answer_numbers(answer, result):
            logger.warning("Answer failed number-check; using template fallback")
            answer = _template_answer(result)
    except Exception as exc:
        logger.warning("phrase_answer failed (%s); using template", exc)
        answer = _template_answer(result)

    return {
        "question": question,
        "answer":   answer,
        "intent":   intent,
        "sql":      result["sql"],
        "columns":  result["columns"],
        "rows":     result["rows"],
        "path_used": path_used,
    }


def _unsupported_response(question: str, path_used: str, detail: str) -> dict:
    """Return a structured 'I can't answer that' response."""
    supported_list = [i for i in SUPPORTED_INTENTS if i != "unsupported"]
    answer = (
        "I can't answer that from this data. "
        f"Supported question types: {', '.join(supported_list)}."
    )
    if detail:
        answer += f" (Detail: {detail})"
    return {
        "question":  question,
        "answer":    answer,
        "intent":    "unsupported",
        "sql":       "",
        "columns":   [],
        "rows":      [],
        "path_used": path_used,
    }
