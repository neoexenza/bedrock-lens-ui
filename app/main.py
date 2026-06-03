"""
bedrock-lens web UI — FastAPI backend
No bedrock-lens library dependency — reads CloudWatch directly.
SQLite history: persists aggregated per-model daily totals locally.
"""
import asyncio
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, date, timezone, timedelta
from typing import AsyncGenerator

import boto3
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

app = FastAPI(title="bedrock-lens UI")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# ── SQLite setup ──────────────────────────────────────────────────────────────
DB_PATH = os.environ.get("DB_PATH", "/data/history.db")

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS daily_usage (
                day        TEXT NOT NULL,
                model_id   TEXT NOT NULL,
                region     TEXT NOT NULL DEFAULT '',
                calls      INTEGER DEFAULT 0,
                input_tokens  INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cost_usd   REAL DEFAULT 0.0,
                PRIMARY KEY (day, model_id, region)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS seen_events (
                event_id TEXT PRIMARY KEY,
                seen_at  INTEGER
            )
        """)
        # Migration: add region column if missing (existing DBs from before this change)
        cols = [r[1] for r in con.execute("PRAGMA table_info(daily_usage)")]
        if "region" not in cols:
            con.execute("ALTER TABLE daily_usage ADD COLUMN region TEXT NOT NULL DEFAULT ''")
            # Rebuild PK by recreating table (SQLite can't alter PK directly)
            con.execute("""
                CREATE TABLE daily_usage_new (
                    day        TEXT NOT NULL,
                    model_id   TEXT NOT NULL,
                    region     TEXT NOT NULL DEFAULT '',
                    calls      INTEGER DEFAULT 0,
                    input_tokens  INTEGER DEFAULT 0,
                    output_tokens INTEGER DEFAULT 0,
                    cost_usd   REAL DEFAULT 0.0,
                    PRIMARY KEY (day, model_id, region)
                )
            """)
            con.execute("INSERT INTO daily_usage_new SELECT day, model_id, region, calls, input_tokens, output_tokens, cost_usd FROM daily_usage")
            con.execute("DROP TABLE daily_usage")
            con.execute("ALTER TABLE daily_usage_new RENAME TO daily_usage")
        con.commit()

@contextmanager
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()

@app.on_event("startup")
def startup():
    init_db()

# ── Pricing ───────────────────────────────────────────────────────────────────
# Pattern-match on partial model ID (per 1M tokens), most-specific first.
_PRICING_TABLE: list[tuple[str, float, float]] = [
    ("claude-opus-4",        5.50,   27.50),
    ("claude-3-opus",       15.00,   75.00),
    ("claude-sonnet-4",      3.30,   16.50),
    ("claude-3-7-sonnet",    3.00,   15.00),
    ("claude-3-5-sonnet",    3.00,   15.00),
    ("claude-3-sonnet",      3.00,   15.00),
    ("claude-haiku-4",       1.10,    5.50),
    ("claude-3-5-haiku",     0.80,    4.00),
    ("claude-3-haiku",       0.25,    1.25),
    ("titan-text-premier",   0.50,    1.50),
    ("titan-text-express",   0.80,    1.60),
    ("titan-text-lite",      0.30,    0.40),
    ("llama3-2-90b",         2.00,    2.00),
    ("llama3-2-11b",         0.16,    0.16),
    ("llama3-1-405b",        5.32,   16.00),
    ("llama3-1-70b",         0.99,    0.99),
    ("llama3-1-8b",          0.22,    0.22),
    ("llama3-70b",           2.65,    3.50),
    ("llama3-8b",            0.30,    0.60),
    ("mistral-large",        4.00,   12.00),
    ("mixtral-8x7b",         0.45,    0.70),
    ("mistral-7b",           0.15,    0.20),
    ("command-r-plus",       3.00,   15.00),
    ("command-r",            0.50,    1.50),
    ("jamba-1-5-large",      2.00,    8.00),
    ("jamba-1-5-mini",       0.20,    0.40),
]

def _lookup_price(model_id: str) -> tuple[float, float] | None:
    lower = model_id.lower()
    for prefix in ("eu.", "us.", "ap.", "us-gov."):
        if lower.startswith(prefix):
            lower = lower[len(prefix):]
            break
    for pattern, in_p, out_p in _PRICING_TABLE:
        if pattern in lower:
            return in_p, out_p
    return None

def cost_for(model_id: str, input_tokens: int, output_tokens: int) -> float | None:
    prices = _lookup_price(model_id)
    if not prices:
        return None
    in_p, out_p = prices
    return round((input_tokens * in_p + output_tokens * out_p) / 1_000_000, 6)

# ── CloudWatch ────────────────────────────────────────────────────────────────
LOG_GROUP     = "/aws/bedrock/model-invocations"
POLL_INTERVAL = 5
OVERLAP_WINDOW = 90

def get_cw_client(region: str, profile: str | None):
    session = boto3.Session(profile_name=profile or None, region_name=region)
    return session.client("logs")

def since_to_ms(since: str) -> int:
    now = int(time.time() * 1000)
    since = since.lower().strip()
    if since == "today":
        d = date.today()
        return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)
    if since == "yesterday":
        d = date.today() - timedelta(days=1)
        return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)
    if since == "week":
        return now - 7 * 86400 * 1000
    unit = since[-1]
    val  = int(since[:-1])
    mult = {"m": 60, "h": 3600, "d": 86400}.get(unit, 3600)
    return now - val * mult * 1000

def fetch_events(cw, start_ms: int, end_ms: int) -> list[dict]:
    events = []
    kwargs = {
        "logGroupName": LOG_GROUP,
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": 10000,
    }
    while True:
        try:
            resp = cw.filter_log_events(**kwargs)
        except cw.exceptions.ResourceNotFoundException:
            break
        for e in resp.get("events", []):
            msg = e.get("message", "").strip()
            if not msg or not msg.startswith("{"):
                continue
            try:
                rec = json.loads(msg)
                events.append({"id": e["eventId"], "record": rec, "ts": e["timestamp"]})
            except (json.JSONDecodeError, KeyError):
                pass
        token = resp.get("nextToken")
        if not token:
            break
        kwargs["nextToken"] = token
    return events

# ── Persist new events to SQLite ──────────────────────────────────────────────
def persist_events(events: list[dict], region: str = ""):
    """Save unseen events into daily_usage and mark them seen."""
    with db() as con:
        seen = {row[0] for row in con.execute("SELECT event_id FROM seen_events")}
        for e in events:
            if e["id"] in seen:
                continue
            rec = e["record"]
            model_id = rec.get("modelId") or rec.get("model_id") or "unknown"
            rec_region = rec.get("region") or region
            input_tokens  = rec.get("input", {}).get("inputTokenCount", 0) or 0
            output_tokens = rec.get("output", {}).get("outputTokenCount", 0) or 0
            c = cost_for(model_id, input_tokens, output_tokens) or 0.0
            day = datetime.fromtimestamp(e["ts"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

            con.execute("""
                INSERT INTO daily_usage (day, model_id, region, calls, input_tokens, output_tokens, cost_usd)
                VALUES (?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(day, model_id, region) DO UPDATE SET
                    calls         = calls + 1,
                    input_tokens  = input_tokens  + excluded.input_tokens,
                    output_tokens = output_tokens + excluded.output_tokens,
                    cost_usd      = cost_usd + excluded.cost_usd
            """, (day, model_id, rec_region, input_tokens, output_tokens, c))

            con.execute(
                "INSERT OR IGNORE INTO seen_events (event_id, seen_at) VALUES (?, ?)",
                (e["id"], int(time.time()))
            )

        # Prune seen_events older than 30 days to keep DB small
        cutoff = int(time.time()) - 30 * 86400
        con.execute("DELETE FROM seen_events WHERE seen_at < ?", (cutoff,))

# ── Aggregate ─────────────────────────────────────────────────────────────────
def aggregate(events: list[dict]) -> dict:
    models: dict[str, dict] = {}
    series_raw: list[dict] = []
    seen: set = set()

    for e in sorted(events, key=lambda x: x["ts"]):
        if e["id"] in seen:
            continue
        seen.add(e["id"])

        rec = e["record"]
        model_id      = rec.get("modelId") or rec.get("model_id") or "unknown"
        input_tokens  = rec.get("input", {}).get("inputTokenCount", 0) or 0
        output_tokens = rec.get("output", {}).get("outputTokenCount", 0) or 0

        if model_id not in models:
            models[model_id] = {"input": 0, "output": 0, "calls": 0, "cost": 0.0}
        models[model_id]["input"]  += input_tokens
        models[model_id]["output"] += output_tokens
        models[model_id]["calls"]  += 1
        c = cost_for(model_id, input_tokens, output_tokens)
        if c:
            models[model_id]["cost"] += c

        series_raw.append({"ts": e["ts"], "cost": c or 0.0})

    # Cumulative cost series (1-min buckets)
    buckets: dict[int, float] = {}
    for s in series_raw:
        bucket = (s["ts"] // 60000) * 60000
        buckets[bucket] = buckets.get(bucket, 0) + s["cost"]
    cumulative, running = [], 0.0
    for ts in sorted(buckets):
        running += buckets[ts]
        cumulative.append({"ts": ts, "cost": round(running, 6)})

    rows = []
    for model_id, stats in sorted(models.items(), key=lambda x: -x[1]["cost"]):
        rows.append({
            "model":    model_id,
            "calls":    stats["calls"],
            "input":    stats["input"],
            "output":   stats["output"],
            "total":    stats["input"] + stats["output"],
            "cost":     f"${stats['cost']:.4f}" if stats["cost"] else "N/A",
            "cost_raw": stats["cost"],
        })

    total_cost = sum(r["cost_raw"] for r in rows)
    return {
        "rows":        rows,
        "total_cost":  f"${total_cost:.4f}",
        "total_calls": sum(r["calls"] for r in rows),
        "series":      cumulative,
        "updated_at":  datetime.now().strftime("%H:%M:%S"),
    }

def aggregate_from_db(start_day: str, end_day: str, region: str = "") -> dict:
    """Return aggregated data from SQLite for date range (for history endpoint)."""
    region_clause = "AND region = ?" if region else ""
    params_base   = (start_day, end_day, region) if region else (start_day, end_day)
    with db() as con:
        rows = con.execute(f"""
            SELECT model_id,
                   SUM(calls) as calls,
                   SUM(input_tokens) as input,
                   SUM(output_tokens) as output,
                   SUM(cost_usd) as cost
            FROM daily_usage
            WHERE day >= ? AND day <= ? {region_clause}
            GROUP BY model_id
            ORDER BY cost DESC
        """, params_base).fetchall()

        daily = con.execute(f"""
            SELECT day, SUM(cost_usd) as cost
            FROM daily_usage
            WHERE day >= ? AND day <= ? {region_clause}
            GROUP BY day ORDER BY day
        """, params_base).fetchall()

    result_rows = []
    for r in rows:
        result_rows.append({
            "model":    r["model_id"],
            "calls":    r["calls"],
            "input":    r["input"],
            "output":   r["output"],
            "total":    r["input"] + r["output"],
            "cost":     f"${r['cost']:.4f}" if r["cost"] else "N/A",
            "cost_raw": r["cost"] or 0.0,
        })

    # Daily cost series for chart
    running, series = 0.0, []
    for d in daily:
        running += d["cost"] or 0.0
        series.append({
            "ts":   int(datetime.strptime(d["day"], "%Y-%m-%d")
                        .replace(tzinfo=timezone.utc).timestamp() * 1000),
            "cost": round(running, 6),
        })

    total_cost = sum(r["cost_raw"] for r in result_rows)
    return {
        "rows":        result_rows,
        "total_cost":  f"${total_cost:.4f}",
        "total_calls": sum(r["calls"] for r in result_rows),
        "series":      series,
        "updated_at":  datetime.now().strftime("%H:%M:%S"),
        "source":      "history",
    }

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/api/usage")
async def get_usage(
    since:   str = Query("today"),
    region:  str = Query(os.environ.get("AWS_DEFAULT_REGION", "eu-central-1")),
    profile: str = Query(None),
):
    cw       = get_cw_client(region, profile)
    now_ms   = int(time.time() * 1000)
    start_ms = since_to_ms(since)
    events   = fetch_events(cw, start_ms, now_ms)
    persist_events(events, region)
    return aggregate(events)


@app.get("/api/history")
async def get_history(
    days:   int = Query(30),
    region: str = Query(""),
):
    """Return aggregated history from SQLite (up to N days back)."""
    end_day   = date.today().strftime("%Y-%m-%d")
    start_day = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    return aggregate_from_db(start_day, end_day, region)


@app.get("/api/history/daily")
async def get_daily_history(region: str = Query("")):
    """Return per-day cost breakdown for chart."""
    region_clause = "WHERE region = ?" if region else ""
    params = (region,) if region else ()
    with db() as con:
        rows = con.execute(f"""
            SELECT day, SUM(cost_usd) as cost, SUM(calls) as calls
            FROM daily_usage {region_clause}
            GROUP BY day ORDER BY day DESC LIMIT 90
        """, params).fetchall()
    return [{"day": r["day"], "cost": round(r["cost"] or 0, 4), "calls": r["calls"]} for r in rows]


async def _live_stream(since: str, region: str, profile: str | None) -> AsyncGenerator:
    cw = get_cw_client(region, profile)
    seen_ids: set = set()

    while True:
        now_ms      = int(time.time() * 1000)
        start_ms    = since_to_ms(since)
        overlap     = now_ms - OVERLAP_WINDOW * 1000
        eff_start   = min(start_ms, overlap)

        events  = fetch_events(cw, eff_start, now_ms)
        new_ids = {e["id"] for e in events} - seen_ids
        seen_ids.update(new_ids)

        if new_ids:
            new_events = [e for e in events if e["id"] in new_ids]
            persist_events(new_events, region)

        data = aggregate(events)
        data["new_ids"] = list(new_ids)
        yield {"data": json.dumps(data)}
        await asyncio.sleep(POLL_INTERVAL)


@app.get("/api/usage/live")
async def live_usage(
    request: Request,
    since:   str = Query("today"),
    region:  str = Query(os.environ.get("AWS_DEFAULT_REGION", "eu-central-1")),
    profile: str = Query(None),
):
    return EventSourceResponse(_live_stream(since, region, profile))
