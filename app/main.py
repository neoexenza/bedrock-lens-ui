"""
bedrock-lens web UI — FastAPI backend
No bedrock-lens library dependency — reads CloudWatch directly.
SQLite history: persists aggregated per-model daily totals + per-event tags locally.
"""
import asyncio
import json
import os
import re
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
        con.execute("""
            CREATE TABLE IF NOT EXISTS event_tags (
                event_id  TEXT NOT NULL,
                tag       TEXT NOT NULL,
                PRIMARY KEY (event_id, tag)
            )
        """)
        # Raw event store for tag extraction and retroactive re-tagging
        con.execute("""
            CREATE TABLE IF NOT EXISTS raw_events (
                event_id      TEXT PRIMARY KEY,
                ts            INTEGER,
                model_id      TEXT,
                region        TEXT DEFAULT '',
                input_tokens  INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cost_usd      REAL DEFAULT 0.0,
                prompt_snippet TEXT DEFAULT ''
            )
        """)
        # Migration: add region column to daily_usage if missing
        cols = [r[1] for r in con.execute("PRAGMA table_info(daily_usage)")]
        if "region" not in cols:
            con.execute("ALTER TABLE daily_usage ADD COLUMN region TEXT NOT NULL DEFAULT ''")
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

# ── Auto-tagger ───────────────────────────────────────────────────────────────
# Tags are namespaced: type:<val>, task:<val>, project:<val>
# Rules are evaluated in order; multiple tags can match.

_CRON_TASK_PATTERNS: list[tuple[str, str]] = [
    (r"morning.briefing|morning brief|Send Max a weekday",  "task:morning-brief"),
    (r"email.monitor|SILENT email check",                   "task:email-monitor"),
    (r"daily.blog.post|blog post",                          "task:blog-post"),
    (r"media.monitor|Radarr.*Sonarr",                       "task:media-monitor"),
    (r"newsletter.digest|newsletter",                       "task:newsletter-digest"),
    (r"homelab.health|healthcheck",                         "task:homelab-health"),
]

_PROJECT_PATTERNS: list[tuple[str, str]] = [
    (r"bedrock.lens",           "project:bedrock-lens-ui"),
    (r"nala|cat.detect",        "project:nala"),
    (r"ring.*camera|snapshot",  "project:ring-camera"),
    (r"national.rail|train",    "project:trains"),
    (r"blog",                   "project:blog"),
]

def extract_tags(rec: dict) -> list[str]:
    """Extract tags from a CloudWatch invocation record."""
    tags: list[str] = []

    # Pull text to search from system prompt + first user message
    system_text = ""
    user_text   = ""
    try:
        body = rec.get("input", {}).get("inputBodyJson", {})
        if isinstance(body, str):
            body = json.loads(body)
        system_parts = body.get("system", [])
        if system_parts:
            system_text = system_parts[0].get("text", "") if isinstance(system_parts[0], dict) else str(system_parts[0])
        messages = body.get("messages", [])
        if messages:
            content = messages[0].get("content", "")
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "text":
                        user_text += c.get("text", "")
            elif isinstance(content, str):
                user_text = content
    except Exception:
        pass

    full_text = system_text + " " + user_text

    # Detect cron origin: [cron:<id> <name>] marker in system prompt
    cron_match = re.search(r"\[cron:[^\]]+\]", full_text)
    if cron_match:
        tags.append("type:cron")
        for pattern, task_tag in _CRON_TASK_PATTERNS:
            if re.search(pattern, full_text, re.IGNORECASE):
                tags.append(task_tag)
    elif "sub-agent" in full_text.lower() or "subagent" in full_text.lower() or "spawned" in full_text.lower():
        tags.append("type:subagent")
    elif full_text.strip():
        # Has a real message — likely a direct Telegram/session invocation
        tags.append("type:telegram")

    # Project tags — apply regardless of type
    for pattern, proj_tag in _PROJECT_PATTERNS:
        if re.search(pattern, full_text, re.IGNORECASE):
            tags.append(proj_tag)

    # Fallback: infer from model if no body was logged
    if not tags:
        model_id = rec.get("modelId","").lower()
        # Sonnet = main session (Telegram). Haiku = cron/subagent but body should have been logged.
        # Embedding models = internal tooling.
        if "sonnet" in model_id:
            tags = ["type:telegram"]
        elif "embed" in model_id:
            tags = ["type:internal"]
        else:
            tags = ["type:unknown"]
    return list(set(tags))


# ── CloudWatch ────────────────────────────────────────────────────────────────
LOG_GROUP      = "/aws/bedrock/model-invocations"
POLL_INTERVAL  = 5
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
    kwargs = {"logGroupName": LOG_GROUP, "startTime": start_ms, "endTime": end_ms, "limit": 10000}
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

# ── Persist ───────────────────────────────────────────────────────────────────
def persist_events(events: list[dict], region: str = ""):
    with db() as con:
        seen = {row[0] for row in con.execute("SELECT event_id FROM seen_events")}
        for e in events:
            if e["id"] in seen:
                continue
            rec           = e["record"]
            model_id      = rec.get("modelId") or rec.get("model_id") or "unknown"
            rec_region    = rec.get("region") or region
            input_tokens  = rec.get("input", {}).get("inputTokenCount", 0) or 0
            output_tokens = rec.get("output", {}).get("outputTokenCount", 0) or 0
            c             = cost_for(model_id, input_tokens, output_tokens) or 0.0
            day           = datetime.fromtimestamp(e["ts"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

            # Daily aggregate
            con.execute("""
                INSERT INTO daily_usage (day, model_id, region, calls, input_tokens, output_tokens, cost_usd)
                VALUES (?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(day, model_id, region) DO UPDATE SET
                    calls         = calls + 1,
                    input_tokens  = input_tokens  + excluded.input_tokens,
                    output_tokens = output_tokens + excluded.output_tokens,
                    cost_usd      = cost_usd + excluded.cost_usd
            """, (day, model_id, rec_region, input_tokens, output_tokens, c))

            # Extract prompt snippet for display
            try:
                body = rec.get("input", {}).get("inputBodyJson", {})
                if isinstance(body, str):
                    body = json.loads(body)
                msgs = body.get("messages", [])
                snippet = ""
                if msgs:
                    content = msgs[0].get("content", "")
                    if isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "text":
                                snippet = part.get("text", "")[:120]
                                break
                    elif isinstance(content, str):
                        snippet = content[:120]
            except Exception:
                snippet = ""

            # Raw event (for retroactive re-tagging)
            con.execute("""
                INSERT OR IGNORE INTO raw_events
                    (event_id, ts, model_id, region, input_tokens, output_tokens, cost_usd, prompt_snippet)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (e["id"], e["ts"], model_id, rec_region, input_tokens, output_tokens, c, snippet))

            # Tags
            tags = extract_tags(rec)
            for tag in tags:
                con.execute(
                    "INSERT OR IGNORE INTO event_tags (event_id, tag) VALUES (?, ?)",
                    (e["id"], tag)
                )

            con.execute(
                "INSERT OR IGNORE INTO seen_events (event_id, seen_at) VALUES (?, ?)",
                (e["id"], int(time.time()))
            )

        # Prune old seen_events
        cutoff = int(time.time()) - 30 * 86400
        con.execute("DELETE FROM seen_events WHERE seen_at < ?", (cutoff,))

# ── Aggregate helpers ─────────────────────────────────────────────────────────
def aggregate(events: list[dict]) -> dict:
    models: dict[str, dict] = {}
    series_raw: list[dict]  = []
    seen: set = set()

    for e in sorted(events, key=lambda x: x["ts"]):
        if e["id"] in seen:
            continue
        seen.add(e["id"])
        rec           = e["record"]
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


def aggregate_from_db(start_day: str, end_day: str, region: str = "", tags: list[str] = []) -> dict:
    region_clause = "AND du.region = ?" if region else ""
    params_base   = [start_day, end_day] + ([region] if region else [])

    tag_join  = ""
    tag_where = ""
    if tags:
        placeholders = ",".join("?" * len(tags))
        tag_join  = f"""
            JOIN (
                SELECT event_id FROM event_tags WHERE tag IN ({placeholders})
                GROUP BY event_id HAVING COUNT(DISTINCT tag) = {len(tags)}
            ) tf ON re.event_id = tf.event_id
        """
        tag_where = f"AND re.event_id IN (SELECT event_id FROM event_tags WHERE tag IN ({placeholders}) GROUP BY event_id HAVING COUNT(DISTINCT tag) = {len(tags)})"
        params_base = params_base + tags + tags  # for both join and where

    with db() as con:
        if tags:
            # Join through raw_events when filtering by tags
            rows = con.execute(f"""
                SELECT re.model_id,
                       SUM(re.input_tokens) as input,
                       SUM(re.output_tokens) as output,
                       COUNT(*) as calls,
                       SUM(re.cost_usd) as cost
                FROM raw_events re
                {tag_join}
                WHERE substr(datetime(re.ts/1000, 'unixepoch'), 1, 10) >= ?
                  AND substr(datetime(re.ts/1000, 'unixepoch'), 1, 10) <= ?
                  {region_clause.replace('du.region', 're.region')}
                GROUP BY re.model_id ORDER BY cost DESC
            """, tags + tags[:len(tags) if region else 0] + [start_day, end_day] + ([region] if region else [])).fetchall()

            daily = con.execute(f"""
                SELECT substr(datetime(re.ts/1000, 'unixepoch'), 1, 10) as day,
                       SUM(re.cost_usd) as cost
                FROM raw_events re
                WHERE re.event_id IN (
                    SELECT event_id FROM event_tags WHERE tag IN ({','.join('?'*len(tags))})
                    GROUP BY event_id HAVING COUNT(DISTINCT tag) = {len(tags)}
                )
                AND substr(datetime(re.ts/1000, 'unixepoch'), 1, 10) >= ?
                AND substr(datetime(re.ts/1000, 'unixepoch'), 1, 10) <= ?
                GROUP BY day ORDER BY day
            """, tags + [start_day, end_day]).fetchall()
        else:
            rows = con.execute(f"""
                SELECT model_id,
                       SUM(calls) as calls,
                       SUM(input_tokens) as input,
                       SUM(output_tokens) as output,
                       SUM(cost_usd) as cost
                FROM daily_usage du
                WHERE day >= ? AND day <= ? {region_clause}
                GROUP BY model_id ORDER BY cost DESC
            """, params_base).fetchall()

            daily = con.execute(f"""
                SELECT day, SUM(cost_usd) as cost
                FROM daily_usage du
                WHERE day >= ? AND day <= ? {region_clause}
                GROUP BY day ORDER BY day
            """, params_base).fetchall()

    result_rows = [{
        "model":    r["model_id"],
        "calls":    r["calls"],
        "input":    r["input"],
        "output":   r["output"],
        "total":    r["input"] + r["output"],
        "cost":     f"${r['cost']:.4f}" if r["cost"] else "N/A",
        "cost_raw": r["cost"] or 0.0,
    } for r in rows]

    running, series = 0.0, []
    for d in daily:
        running += d["cost"] or 0.0
        series.append({
            "ts":   int(datetime.strptime(d["day"], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000),
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
    days:   int  = Query(30),
    region: str  = Query(""),
    tags:   str  = Query(""),   # comma-separated, e.g. "type:cron,task:morning-brief"
):
    end_day   = date.today().strftime("%Y-%m-%d")
    start_day = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    tag_list  = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    return aggregate_from_db(start_day, end_day, region, tag_list)


@app.get("/api/history/daily")
async def get_daily_history(region: str = Query("")):
    region_clause = "WHERE region = ?" if region else ""
    params = (region,) if region else ()
    with db() as con:
        rows = con.execute(f"""
            SELECT day, SUM(cost_usd) as cost, SUM(calls) as calls
            FROM daily_usage {region_clause}
            GROUP BY day ORDER BY day DESC LIMIT 90
        """, params).fetchall()
    return [{"day": r["day"], "cost": round(r["cost"] or 0, 4), "calls": r["calls"]} for r in rows]


@app.get("/api/tags")
async def get_tags():
    """Return all known tags with their total cost and call count."""
    with db() as con:
        rows = con.execute("""
            SELECT et.tag,
                   COUNT(DISTINCT et.event_id) as calls,
                   SUM(re.cost_usd) as cost
            FROM event_tags et
            JOIN raw_events re ON re.event_id = et.event_id
            GROUP BY et.tag
            ORDER BY cost DESC
        """).fetchall()
    return [{"tag": r["tag"], "calls": r["calls"], "cost": round(r["cost"] or 0, 4)} for r in rows]


@app.get("/api/allocation")
async def get_allocation(
    days:      int = Query(30),
    namespace: str = Query("type"),   # type | task | project
):
    """Return cost allocation grouped by tag namespace for pie chart."""
    start_day = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    end_day   = date.today().strftime("%Y-%m-%d")
    prefix    = namespace + ":"
    with db() as con:
        rows = con.execute("""
            SELECT et.tag,
                   COUNT(DISTINCT et.event_id) as calls,
                   SUM(re.cost_usd) as cost
            FROM event_tags et
            JOIN raw_events re ON re.event_id = et.event_id
            WHERE et.tag LIKE ?
              AND substr(datetime(re.ts/1000, 'unixepoch'), 1, 10) >= ?
              AND substr(datetime(re.ts/1000, 'unixepoch'), 1, 10) <= ?
            GROUP BY et.tag
            ORDER BY cost DESC
        """, (prefix + "%", start_day, end_day)).fetchall()
    return [{"tag": r["tag"], "label": r["tag"].replace(prefix, ""), "calls": r["calls"], "cost": round(r["cost"] or 0, 4)} for r in rows]


async def _live_stream(since: str, region: str, profile: str | None) -> AsyncGenerator:
    cw = get_cw_client(region, profile)
    seen_ids: set = set()
    while True:
        now_ms    = int(time.time() * 1000)
        start_ms  = since_to_ms(since)
        overlap   = now_ms - OVERLAP_WINDOW * 1000
        eff_start = min(start_ms, overlap)
        events    = fetch_events(cw, eff_start, now_ms)
        new_ids   = {e["id"] for e in events} - seen_ids
        seen_ids.update(new_ids)
        if new_ids:
            persist_events([e for e in events if e["id"] in new_ids], region)
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


@app.post("/api/tags/{event_id}")
async def add_tag(event_id: str, body: dict):
    """Manually add a tag to an event."""
    tag = body.get("tag", "").strip()
    if not tag:
        return {"error": "tag required"}
    with db() as con:
        con.execute("INSERT OR IGNORE INTO event_tags (event_id, tag) VALUES (?, ?)", (event_id, tag))
    return {"ok": True}
