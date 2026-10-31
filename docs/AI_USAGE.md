# AI Usage — Mini AI BI Assistant

---

## 1. AI Tools Used

- **Antigravity (Google DeepMind)** — primary coding assistant used throughout the project for code generation, design decisions, and debugging
- **Google Gemini 2.0 Flash** — used as the runtime LLM inside the application itself for intent classification and answer phrasing
- **GitHub Copilot** — occasional inline autocomplete while writing test cases and SQL queries

---

## 2. How I Used AI During Development

I used Antigravity heavily across all phases of development. Here's a concrete breakdown:

**Project structure & scaffolding**
I started by describing the overall goal to Antigravity and asking it to suggest a clean Python project structure. It proposed the `app/`, `scripts/`, `tests/`, `static/`, and `docs/` layout, which I accepted with minor tweaks. This saved me maybe 30 minutes of setup.

**Data ingestion (`scripts/load_data.py`)**
I wrote the validation logic mostly myself, but used Antigravity to help me think through edge cases. For example, I asked:
> *"What validation rules would you apply to a sales CSV where discount_amount could be blank or missing?"*
It suggested treating blank as 0 and rejecting negatives — which matched my intuition. The `parse_date()` helper and the idempotent delete-before-insert pattern both came from a prompt asking how to make the loader re-runnable safely.

**Analytics layer (`app/analytics.py`)**
I wrote the SQL queries myself but used Copilot autocomplete to fill in the `RANK() OVER (ORDER BY ...)` window function syntax for `top_product_by_revenue`. For `revenue_change_between_months`, I asked Antigravity how to do it in a single query rather than two round trips — it suggested the conditional aggregation approach (`SUM(CASE WHEN ... THEN revenue ELSE 0 END)`) which was cleaner than what I had in mind.

**AI safety layer (`app/ai.py`)**
This was the most complex part. I asked Antigravity:
> *"How do I safely execute LLM-generated SQL against SQLite without risking injection or destructive queries?"*
It walked me through the layered approach: keyword blocklist first (fast), then sqlglot parse, then table whitelist, then read-only connection. I implemented all of it myself but the architecture came from that conversation. The `_verify_answer_numbers()` hallucination guard was my own idea, but I asked AI to help write the regex for extracting numbers from free text.

**Tests (`tests/`)**
I used Antigravity to generate the initial `conftest.py` fixture structure — specifically the pattern of patching `DB_PATH` to redirect to an in-memory test database without modifying application code. I then wrote most of the individual test cases myself. The `EXPECTED_TOTAL_REVENUE` manual calculation in conftest was done by hand to make the test self-documenting.

---

## 3. Problems Encountered

**1. sqlglot version compatibility**
The `validate_sql()` function initially failed because `sqlglot.parse()` was returning a slightly different AST structure in newer versions. `stmt.args.get("limit")` wasn't matching as expected. Took me about 20 minutes to isolate this.

**2. SQLite URI mode on Windows**
The read-only connection using `file:path?mode=ro` URI syntax failed on Windows because backslashes in the path broke the URI. I spent time debugging a confusing `sqlite3.OperationalError: unable to open database file` before realising the path separator issue.

**3. Gemini SDK import structure**
`google-genai` changed its import paths between SDK versions. The code that worked with `google.generativeai` didn't work with `google.genai`. The lazy import inside `GeminiClient.__init__` was intentional — it prevents a crash on import when the SDK isn't installed.

**4. Test DB isolation**
My first version of `conftest.py` used a module-level DB path override which leaked state between test files. Switching to a `session`-scoped fixture with explicit `monkeypatch`-style attribute overrides fixed the isolation issue.

**5. Pydantic v2 field validators**
`@validator` (Pydantic v1) doesn't work in Pydantic v2. I had to switch to `@field_validator` with the `@classmethod` decorator. The error message was cryptic at first — Antigravity pointed me to the v2 migration docs.

---

## 4. How I Debugged Them

**sqlglot issue**: I added `print(type(stmt), stmt.args)` directly in `validate_sql()` to inspect the parsed AST. Once I saw the actual key names, the fix was obvious.

**SQLite URI on Windows**: I added a logging statement printing the exact URI string being passed to `sqlite3.connect()`. Seeing `file:c:\Users\...\sales.db?mode=ro` immediately showed the backslash problem. I fixed it by replacing `chr(92)` (backslash) with `/` in the URI.

**Gemini SDK imports**: I used `pip show google-genai` to check the installed version and read the changelog. The lazy import pattern (`from google import genai` inside `__init__`) was suggested by Antigravity as a way to defer the import error until the class is actually instantiated, so tests can still run offline.

**Test isolation**: I ran `pytest -v --tb=short` and noticed certain tests were passing only when run in isolation but failing in a full suite run. This pointed to shared state. I then read through the fixtures and spotted the global DB_PATH mutation.

**Pydantic v2**: I ran `pytest -v` and got `PydanticUserError: `@validator` is no longer supported`. Searched the Pydantic v2 migration guide, confirmed the new `@field_validator` API, updated accordingly.

For most issues I tried to isolate the bug with the smallest possible reproduction before asking AI for help. When I did ask, I gave the exact error message and the relevant code snippet — vague prompts got vague answers.

---

## 5. What I Would Improve With Another Day of Work

1. **Chart visualisation**: Add a Chart.js line chart to the UI for the monthly revenue trend response — the table is functional but a chart would be more useful for a BI tool.

2. **More analytics intents**: Add breakdowns by `category` and `sales_channel`, and a top-N filter (e.g., "top 3 products"). The current five intents cover the basics but a real BI tool needs more.

3. **Streaming responses**: Stream the LLM's phrased answer token-by-token to the UI so it feels faster and more interactive, rather than waiting for the full response before showing anything.

4. **Proper RAG retrieval step**: Replace the keyword fallback with a small vector store of supported intent descriptions. This would handle synonym and paraphrase variations much more robustly than regex keyword matching.

5. **Result caching with TTL**: The analytics queries run against a small static dataset, so caching results for a few minutes would reduce repeated SQLite hits with no downside.

6. **Better SQL validator coverage**: The current validator misses some SQL injection patterns like subqueries that read from `sqlite_master`. A stricter AST walk-through would catch more edge cases.

7. **Authentication**: The API has no auth layer — anyone who can reach the server can query the data. Even a simple API key header would make it more suitable for a real deployment.
