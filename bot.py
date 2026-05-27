#!/usr/bin/env python3
"""
Next Step — Executive Function Telegram Bot for AuDHD
Assistant: Jamie | Powered by DeepSeek API

ARCHITECTURE:
  Single unified conversation pipeline with conversation history,
  tool loop for HD data, and natural task + Human Design coaching.

PORTABLE: All paths and IDs configured via environment variables.
  See README.md for deployment options.

ENV VARS:
  TELEGRAM_BOT_TOKEN    (required)  Telegram bot token from @BotFather
  DEEPSEEK_API_KEY      (required)  DeepSeek API key
  NEXTSTEP_MCP_SRC      (optional)  Path to MCP server src/ dir (default: ./mcp-server/src)
  NEXTSTEP_FAMILY_PATH  (optional)  Path to family.json (default: ./family.json)
  NEXTSTEP_DB_PATH      (optional)  Path to SQLite DB (default: ./data/next_step.db)
  NEXTSTEP_NAME         (optional)  Assistant name (default: Jamie)
  NEXTSTEP_PROFILE      (optional)  Instance identifier for logging (default: next-step)
  DEEPSEEK_BASE_URL     (optional)  DeepSeek API base URL (default: https://api.deepseek.com)
"""
import os
import sys
import json
import sqlite3
import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from openai import OpenAI

# ── Portable Config ──────────────────────────────────────────────
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is required")

# MCP Server path — where to find the Human Design calculation engine
MCP_SRC = os.environ.get(
    "NEXTSTEP_MCP_SRC",
    str(Path(__file__).parent / "mcp-server" / "src")
)
if not Path(MCP_SRC).is_dir():
    logger_warn = logging.getLogger("next-step")
    logger_warn.warning(f"MCP server not found at {MCP_SRC} — HD tools will be unavailable")

# Family data path
FAMILY_PATH = Path(os.environ.get(
    "NEXTSTEP_FAMILY_PATH",
    str(Path(__file__).parent / "family.json")
))

# Database path (creates parent dirs automatically)
DB_PATH = Path(os.environ.get(
    "NEXTSTEP_DB_PATH",
    str(Path(__file__).parent / "data" / "next_step.db")
))

# Identity
ASSISTANT_NAME = os.environ.get("NEXTSTEP_NAME", "Jamie")
INSTANCE_PROFILE = os.environ.get("NEXTSTEP_PROFILE", "next-step")

# AI Provider (OpenAI-compatible)
PROVIDER_API_KEY = os.environ.get("NEXTSTEP_API_KEY") or os.environ.get("DEEPSEEK_API_KEY", "")
PROVIDER_BASE_URL = os.environ.get("NEXTSTEP_BASE_URL") or os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
PROVIDER_MODEL = os.environ.get("NEXTSTEP_MODEL", "deepseek-chat")
client = OpenAI(api_key=PROVIDER_API_KEY, base_url=PROVIDER_BASE_URL) if PROVIDER_API_KEY else None

# ── Family Data ──────────────────────────────────────────────────
_family_data = {}
_active_profile = os.environ.get("NEXTSTEP_ACTIVE_PROFILE", "michael")

# Lazy MCP import helper — adds MCP_SRC to sys.path once
_mcp_path_added = False
def _ensure_mcp_path():
    global _mcp_path_added
    if not _mcp_path_added and Path(MCP_SRC).is_dir():
        sys.path.insert(0, MCP_SRC)
        _mcp_path_added = True

def _load_family():
    global _family_data, _active_profile
    try:
        with open(FAMILY_PATH) as f:
            data = json.load(f)
            _family_data = data.get("family", {})
            _active_profile = data.get("active", "michael")
    except Exception:
        _family_data = {}

def _get_active_birth():
    _load_family()
    member = _family_data.get(_active_profile, {})
    if member:
        return {
            "name": member.get("name", "Michael"),
            "year": member["year"], "month": member["month"],
            "day": member["day"], "hour": member["hour"],
            "location": member.get("location", "UTC"),
            "lat": member.get("lat", 0), "lon": member.get("lon", 0),
        }
    # Ultimate fallback
    return {
        "name": "Michael", "year": 1989, "month": 12, "day": 10,
        "hour": 17.1167, "location": "Simi Valley CA",
        "lat": 34.2694, "lon": -118.7815,
    }

def _set_active_profile(profile: str) -> bool:
    global _active_profile
    _load_family()
    if profile in _family_data:
        _active_profile = profile
        # Persist to file
        try:
            with open(FAMILY_PATH) as f:
                data = json.load(f)
            data["active"] = profile
            with open(FAMILY_PATH, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass
        return True
    return False

# ── Logging ─────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    stream=sys.stderr,
)
logger = logging.getLogger("next-step")

# ── Unified Jamie System Prompt ──────────────────────────────────
SOUL_PATH = Path(__file__).parent / "SOUL.md"
SOUL_PROMPT = None

def _load_soul() -> str:
    """Load Jamie's soul prompt from SOUL.md, with current state injected."""
    global SOUL_PROMPT
    if SOUL_PROMPT is None:
        try:
            with open(SOUL_PATH) as f:
                SOUL_PROMPT = f.read()
            # Replace "Fred" with the configured assistant name
            SOUL_PROMPT = SOUL_PROMPT.replace("Fred", ASSISTANT_NAME)
        except Exception:
            SOUL_PROMPT = f"You are {ASSISTANT_NAME}, an executive function and Human Design assistant."
    return SOUL_PROMPT


# ── Database ─────────────────────────────────────────────────────
def get_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            raw_dump_parent_id INTEGER,
            description TEXT NOT NULL,
            micro_step_current TEXT,
            status TEXT DEFAULT 'pending',
            position INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            completed_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS state (
            user_id INTEGER PRIMARY KEY,
            current_task_id INTEGER,
            user_name TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS raw_dumps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            raw_text TEXT NOT NULL,
            parsed_task_count INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversation_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conv_user ON conversation_history(user_id, created_at)")
    conn.commit()
    return conn


# ── Conversation History ─────────────────────────────────────────
MAX_HISTORY = 20  # last N exchanges to keep per user

def get_conversation_history(conn, user_id: int) -> list[dict]:
    """Return the last MAX_HISTORY messages for this user as {"role": str, "content": str}."""
    cur = conn.execute(
        "SELECT role, content FROM conversation_history WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, MAX_HISTORY * 2)  # up to N pairs
    )
    rows = cur.fetchall()
    rows.reverse()  # oldest first
    return [{"role": r, "content": c} for r, c in rows]

def save_conversation_turn(conn, user_id: int, user_msg: str, assistant_msg: str):
    """Persist one user+assistant exchange."""
    conn.execute(
        "INSERT INTO conversation_history (user_id, role, content) VALUES (?, 'user', ?)",
        (user_id, user_msg)
    )
    conn.execute(
        "INSERT INTO conversation_history (user_id, role, content) VALUES (?, 'assistant', ?)",
        (user_id, assistant_msg)
    )
    # Prune old history beyond MAX_HISTORY
    conn.execute("""
        DELETE FROM conversation_history WHERE user_id = ? AND id NOT IN (
            SELECT id FROM conversation_history WHERE user_id = ? ORDER BY id DESC LIMIT ?
        )
    """, (user_id, user_id, MAX_HISTORY * 2))
    conn.commit()


# ── Tool Execution for Jamie ─────────────────────────────────────
def _execute_tool(tool_line: str, user_id: int, display_name: str) -> str:
    """
    Execute a Jamie tool request and return the result text.
    Format: [TOOL:name:arg1,arg2,...]
    """
    import re
    m = re.match(r'\[TOOL:(\w+):?(.*?)\]', tool_line.strip())
    if not m:
        return "[Tool parse error]"
    
    tool_name = m.group(1)
    args_str = m.group(2)
    args = [a.strip() for a in args_str.split(",") if a.strip()] if args_str else []
    
    logger.info(f"Jamie requested tool: {tool_name} args={args}")
    
    try:
        _ensure_mcp_path()
        from mcp_server import get_deep_context, get_relationship_composite
        from cosmic_calculator import calculate_natal_chart
        from ephemeris_engine import init_ephemeris
        init_ephemeris()
        
        # ── deep_context ──
        if tool_name == "deep_context":
            profile_a = args[0] if args else _active_profile
            profile_b = args[1] if len(args) > 1 else None
            
            if profile_b:
                result = get_deep_context(profile_a, profile_b)
            else:
                result = get_deep_context(profile_a)
            
            # If no HD data yet (profile not in family.json with birth data),
            # fall back to active profile's birth data
            if "error" in result and profile_a == _active_profile:
                b = _get_active_birth()
                from datetime import datetime as dt
                from geo_resolver import local_to_utc
                utc = local_to_utc(b["year"], b["month"], b["day"], b["hour"], b["location"])
                birth_dt = dt(utc[0], utc[1], utc[2], int(utc[3]), int((utc[3] % 1) * 60))
                chart = calculate_natal_chart(
                    name=b["name"], birth_dt=birth_dt,
                    lat=b.get("lat", 0), lon=b.get("lon", 0),
                    timezone="America/Los_Angeles"
                )
                result = {"chart": chart, "note": "Deep context via direct calculation"}
            
            return json.dumps(result, indent=2, default=str)
        
        # ── transits ──
        elif tool_name == "transits":
            profile = args[0] if args else _active_profile
            result = get_deep_context(profile)
            if "error" not in result and "transits" in result:
                return json.dumps({"transits": result["transits"]}, indent=2, default=str)
            return json.dumps(result, indent=2, default=str)
        
        # ── relate ──
        elif tool_name == "relate":
            if len(args) < 2:
                return json.dumps({"error": "Need two profile names"})
            result = get_relationship_composite(args[0], args[1])
            return json.dumps(result, indent=2, default=str)
        
        # ── map ──
        elif tool_name == "map":
            profile = args[0] if args else _active_profile
            b = _get_active_birth()
            from astro_cartography import calculate_cartography_lines
            from ephemeris_engine import julday
            from geo_resolver import local_to_utc
            utc = local_to_utc(b["year"], b["month"], b["day"], b["hour"], b["location"])
            jd = julday(utc[0], utc[1], utc[2], utc[3])
            lines = calculate_cartography_lines(jd)
            # Return top locations summary
            top = []
            for line in lines[:10]:
                top.append(f"{line.get('planet','?')} {line.get('angle','?')}: {line.get('lat',0):.1f}°, {line.get('lon',0):.1f}°")
            return json.dumps({"profile": profile, "top_lines": top, "total_lines": len(lines)}, indent=2)
        
        # ── list (tasks) ──
        elif tool_name == "list":
            conn2 = get_db()
            tasks = get_all_pending(conn2, user_id)
            conn2.close()
            if not tasks:
                return "No pending tasks."
            return "\n".join(f"{i+1}. {t[1]}" for i, t in enumerate(tasks))
        
        # ── done ──
        elif tool_name == "done":
            conn2 = get_db()
            next_task = complete_current(conn2, user_id)
            conn2.close()
            if next_task:
                return f"Task marked complete. Next: {next_task[1]}"
            return "Task marked complete. Queue is empty!"
        
        else:
            return f"[Unknown tool: {tool_name}]"
    
    except Exception as e:
        logger.exception(f"Tool execution error ({tool_name}): {e}")
        return f"[Tool error: {str(e)[:200]}]"


# ── Task Management ─────────────────────────────────────────────
def get_or_create_state(conn, user_id):
    cur = conn.execute("SELECT current_task_id, user_name FROM state WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if not row:
        conn.execute("INSERT INTO state (user_id) VALUES (?)", (user_id,))
        conn.commit()
        return None, None
    return row[0], row[1]


def save_raw_dump(conn, user_id, raw_text, task_count):
    cur = conn.execute(
        "INSERT INTO raw_dumps (user_id, raw_text, parsed_task_count) VALUES (?, ?, ?)",
        (user_id, raw_text, task_count)
    )
    conn.commit()
    return cur.lastrowid


def add_tasks(conn, user_id, descriptions, raw_dump_id=None):
    """Add parsed tasks and return (first_task_id, first_task_description)."""
    cur = conn.execute("SELECT MAX(position) FROM tasks WHERE user_id = ?", (user_id,))
    max_pos = cur.fetchone()[0] or 0
    
    first_id = None
    first_desc = None
    
    for desc in descriptions:
        max_pos += 1
        cur = conn.execute(
            "INSERT INTO tasks (user_id, raw_dump_parent_id, description, position) VALUES (?, ?, ?, ?)",
            (user_id, raw_dump_id, desc.strip(), max_pos)
        )
        if first_id is None:
            first_id = cur.lastrowid
            first_desc = desc.strip()
    
    conn.commit()
    return first_id, first_desc


def get_next_task(conn, user_id):
    cur = conn.execute(
        "SELECT id, description FROM tasks WHERE user_id = ? AND status = 'pending' ORDER BY position LIMIT 1",
        (user_id,)
    )
    return cur.fetchone()


def complete_current(conn, user_id):
    current_id, _ = get_or_create_state(conn, user_id)
    if current_id:
        conn.execute(
            "UPDATE tasks SET status = 'completed', completed_at = datetime('now') WHERE id = ?",
            (current_id,)
        )
    next_task = get_next_task(conn, user_id)
    if next_task:
        conn.execute("UPDATE state SET current_task_id = ? WHERE user_id = ?", (next_task[0], user_id))
    else:
        conn.execute("UPDATE state SET current_task_id = NULL WHERE user_id = ?", (user_id,))
    conn.commit()
    return next_task


def get_all_pending(conn, user_id):
    cur = conn.execute(
        "SELECT position, description FROM tasks WHERE user_id = ? AND status = 'pending' ORDER BY position",
        (user_id,)
    )
    return cur.fetchall()


# ── Jamie's Unified Conversation ─────────────────────────────────
def _extract_tasks_from_text(text: str) -> list[str]:
    """
    Fast, local task extraction without an extra AI call.
    Splits on newlines/bullets/commas, filters garbage.
    """
    import re
    lines = []
    # Split on common delimiters
    for chunk in re.split(r'[\n;•·▪▸►●○◉]|, (?=[A-Z])', text):
        chunk = chunk.strip().lstrip("-*•0123456789. )] ")
        # Filter out conversational filler
        if not chunk or len(chunk) < 5:
            continue
        if chunk.lower().startswith(("hey", "hi ", "hello", "jamie", "thanks", "ok ")):
            continue
        lines.append(chunk)
    return lines


def _call_jamie(messages: list[dict], max_tokens: int = 500, temperature: float = 0.7) -> str:
    """Call DeepSeek with the given messages and return the response text."""
    if not client:
        return "Hey! I'm having trouble connecting to my brain right now. Give me a moment?"
    
    response = client.chat.completions.create(
        model=PROVIDER_MODEL,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return response.choices[0].message.content.strip()


# ── Telegram Handlers ────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = user.first_name
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO state (user_id, user_name) VALUES (?, ?)", (user.id, name))
    conn.commit()
    
    # Clear old conversation history for fresh start
    conn.execute("DELETE FROM conversation_history WHERE user_id = ?", (user.id,))
    conn.commit()
    conn.close()
    
    await update.message.reply_text(
        f"Hey {name}! 👋 I'm **{ASSISTANT_NAME}**, your personal assistant.\n\n"
        "I can help you stay on top of tasks, and I understand your Human Design deeply. "
        "Just talk to me naturally — dump tasks, ask about your chart, whatever's on your mind.\n\n"
        "**Quick tips:**\n"
        "• Dump your chaos — I'll give you ONE thing to do at a time\n"
        "• Say **done** and I'll celebrate + serve the next step\n"
        "• Ask me about your chart, transits, or compatibility anytime\n"
        "• I'll never show the full list unless you ask\n\n"
        "Ready when you are! 🚀"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"**{ASSISTANT_NAME} — Your Assistant**\n\n"
        "**Task Commands:**\n"
        "/start — Fresh start\n"
        "/list — See pending tasks\n"
        "/status — Current task\n\n"
        "**Human Design:**\n"
        "/chart — Your bodygraph\n"
        "/map — Astrocartography\n"
        "/where [career|love|family] — Best locations\n"
        "/who — Family profiles\n"
        "/relate [name] — Compatibility\n\n"
        "**Or just talk to me naturally.** Dump tasks, ask questions, "
        "explore your chart — I flow between everything smoothly."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id
    name = update.effective_user.first_name
    
    try:
        await _handle_message_impl(update, context, text, user_id, name)
    except Exception as e:
        logger.exception(f"FATAL in message handler: {e}")
        try:
            await update.message.reply_text(
                f"⚠️ I hit a snag. Try again?\nError: {str(e)[:200]}"
            )
        except Exception:
            logger.error("Could not send error reply")


async def _handle_message_impl(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                text: str, user_id: int, name: str):
    conn = get_db()
    current_id, stored_name = get_or_create_state(conn, user_id)
    display_name = stored_name or name
    
    # ── Fast-path: explicit /list ──
    if text.lower().strip().startswith("/list") or text.lower().strip() == "list":
        tasks = get_all_pending(conn, user_id)
        if not tasks:
            conn.close()
            await update.message.reply_text("Queue is empty! 🎉 Want to dump what's on your mind?")
        else:
            task_list = "\n".join(f"{i+1}. {t[1]}" for i, t in enumerate(tasks))
            conn.close()
            await update.message.reply_text(f"**Your pending tasks:**\n\n{task_list}\n\nReply 'done' when you complete the current one!")
        return
    
    # ── Fast-path: explicit /status ──
    if text.lower().strip().startswith("/status") or text.lower().strip() == "status":
        if current_id:
            cur = conn.execute("SELECT description, micro_step_current FROM tasks WHERE id = ?", (current_id,))
            row = cur.fetchone()
            if row:
                step = row[1] or row[0]
                conn.close()
                await update.message.reply_text(f"You're working on: **{step}**\n\nReply 'done' when complete!")
                return
        conn.close()
        await update.message.reply_text("No current task. Dump some tasks to begin!")
        return
    
    # ── Fast-path: explicit "done" ──
    if _looks_like_done(text):
        next_task = complete_current(conn, user_id)
        if next_task:
            # Re-read current state after completion
            current_id, _ = get_or_create_state(conn, user_id)
        else:
            conn.close()
            await update.message.reply_text("🎉 Queue cleared! You're all done. Want to add something new?")
            return
    
    # Get current task for context injection
    current_task_desc = None
    if current_id:
        cur = conn.execute(
            "SELECT micro_step_current, description FROM tasks WHERE id = ?", (current_id,)
        )
        row = cur.fetchone()
        if row:
            current_task_desc = row[0] or row[1]
    
    # ── Build the messages array ──
    soul = _load_soul()
    
    # Inject current state into the system prompt
    state_context = f"\n\n[CURRENT STATE]\nActive profile: {_active_profile} ({display_name})"
    if current_task_desc:
        state_context += f"\nCurrent task: {current_task_desc}"
    else:
        state_context += "\nNo current task. Queue is empty."
    state_context += f"\nFamily profiles available: {', '.join(_family_data.keys())}"
    state_context += f"\nToday is {datetime.now(timezone.utc).strftime('%A, %B %d %Y, %H:%M UTC')}"
    
    # ── Silent HD pre-fetch ──
    # If no recent conversation history, inject HD data silently
    # so Jamie wakes up already knowing the chart — no visible tool call
    hd_context = _fetch_silent_hd_context(conn, user_id)
    if hd_context:
        state_context += "\n\n" + hd_context
    
    # ── Journal context ──
    journal_context = _get_recent_journal_context(user_id)
    if journal_context:
        state_context += "\n\n" + journal_context
    
    messages = [
        {"role": "system", "content": soul + state_context}
    ]
    
    # Add conversation history
    history = get_conversation_history(conn, user_id)
    messages.extend(history)
    
    # Add the user's message
    messages.append({"role": "user", "content": text})
    
    # ── Call Jamie ──
    logger.info(f"[{display_name}] Unified Jamie processing: {text[:80]}")
    response = _call_jamie(messages, max_tokens=500)
    
    # ── Tool loop: Jamie can request data ──
    tool_loop_guard = 0
    while "[TOOL:" in response and tool_loop_guard < 4:
        tool_loop_guard += 1
        
        # Extract the first tool line
        tool_start = response.index("[TOOL:")
        tool_end = response.index("]", tool_start) + 1
        tool_line = response[tool_start:tool_end]
        
        # Execute the tool
        tool_result = _execute_tool(tool_line, user_id, display_name)
        
        # Inject tool result into conversation
        messages.append({"role": "assistant", "content": response})
        messages.append({"role": "user", "content": f"[Tool result for {tool_line}]\n{tool_result}"})
        
        # Call Jamie again with the tool data
        response = _call_jamie(messages, max_tokens=600)
    
    # ── Post-process: if Jamie suggests tasks, store them ──
    # Check if response contains task-like structure (numbered list, bullet actions)
    # and if the user's message looks like a brain dump
    is_dump = _looks_like_task_dump(text)
    if is_dump:
        tasks_found = _extract_tasks_from_text(text)
        if tasks_found:
            dump_id = save_raw_dump(conn, user_id, text, len(tasks_found))
            first_id, first_desc = add_tasks(conn, user_id, tasks_found, dump_id)
            if first_id:
                conn.execute(
                    "UPDATE state SET current_task_id = ? WHERE user_id = ?",
                    (first_id, user_id)
                )
                conn.commit()
                logger.info(f"[{display_name}] Auto-parsed {len(tasks_found)} tasks from dump")
    
    # ── Extract journal entries from Jamie's response ──
    import re as _re
    journal_match = _re.search(r'\[JOURNAL:\s*(.+?)\]', response, _re.DOTALL)
    if journal_match:
        journal_text = journal_match.group(1).strip()
        response = response[:journal_match.start()] + response[journal_match.end():]
        response = response.strip()
        _append_journal_entry(user_id, journal_text)
    
    # ── Save conversation turn ──
    save_conversation_turn(conn, user_id, text, response)
    conn.close()
    
    # ── Send response ──
    await update.message.reply_text(response)


def _looks_like_task_dump(text: str) -> bool:
    """Heuristic: does this look like someone dumping tasks?"""
    t = text.strip()
    # Multi-line, bullet points, or lots of punctuation-separated items
    line_count = len([l for l in t.split("\n") if len(l.strip()) > 10])
    if line_count >= 2:
        return True
    if t.startswith("-") or t.startswith("*") or t.startswith("•"):
        return True
    # Comma-separated task-like phrases
    if t.count(",") >= 2 and len(t) > 80:
        return True
    return False


def _looks_like_done(text: str) -> bool:
    """Heuristic: is this a task completion signal?"""
    t = text.lower().strip()
    done_words = ("done", "✅", "✔️", "finished", "complete", "completed", "did it", "finished it", "all done", "that's done")
    return t in done_words or any(t.startswith(w) for w in done_words)


# ── Silent HD Context Injection ──────────────────────────────────
# Cache: only re-fetch HD data every 6 hours per profile
_hd_cache = {}  # {profile_key: (timestamp, context_string)}

def _fetch_silent_hd_context(conn, user_id: int) -> str | None:
    """
    Silently fetch Human Design deep_context and return a compact,
    jargon-free context block for injection into the system prompt.
    Cached per profile for 6 hours. Returns None if MCP unavailable.
    """
    global _hd_cache
    
    profile = _active_profile
    now = datetime.now(timezone.utc)
    
    # Check cache
    if profile in _hd_cache:
        cached_time, cached_text = _hd_cache[profile]
        if (now - cached_time).total_seconds() < 21600:  # 6 hours
            return cached_text
    
    # Check if this session is "fresh" — no conversation in last 2 hours
    cur = conn.execute(
        "SELECT MAX(created_at) FROM conversation_history WHERE user_id = ?",
        (user_id,)
    )
    row = cur.fetchone()
    if row and row[0]:
        try:
            last_msg = datetime.fromisoformat(row[0])
            if (now - last_msg).total_seconds() < 7200:  # 2 hours
                return None  # Active conversation, don't re-inject
        except Exception:
            pass
    
    # Fetch HD data
    try:
        _ensure_mcp_path()
        from mcp_server import get_deep_context
        from ephemeris_engine import init_ephemeris
        init_ephemeris()
        
        data = get_deep_context(profile)
        if "error" in data:
            return None
        
        chart = data.get("chart", {})
        transits = data.get("transits", {})
        conditioning = transits.get("conditioning", {})
        circuit = data.get("circuit_analysis", {})
        astro = data.get("astro_hd", {})
        
        # Compute open centers from all 9
        all_centers = ["Head", "Ajna", "Throat", "G", "Heart/Ego", 
                       "Solar Plexus", "Spleen", "Sacral", "Root"]
        defined = set(chart.get("defined_centers", []))
        open_centers = [c for c in all_centers if c not in defined]
        
        # Extract cross name from dict
        cross_data = chart.get("incarnation_cross", {})
        cross_name = cross_data if isinstance(cross_data, str) else cross_data.get("name", "Unknown")
        
        # Build a compact, jargon-free context block
        lines = [
            "[SILENT HD CONTEXT — DO NOT OUTPUT RAW DATA TO USER]",
            "",
            f"Active Profile: {profile}",
            f"Type: {chart.get('type', 'Unknown')} — Strategy: {chart.get('strategy', '')}",
            f"Authority: {chart.get('authority', '')}",
            f"Profile: {chart.get('profile', '')}",
            f"Cross: {cross_name}",
            f"Defined Centers: {', '.join(chart.get('defined_centers', []))}",
            f"Open Centers: {', '.join(open_centers)}",
        ]
        
        channels = chart.get("defined_channels", [])
        if channels:
            lines.append("Channels: " + ", ".join(
                f"{c['gates'][0]}-{c['gates'][1]} ({c['name']})" for c in channels
            ))
        
        # Circuit analysis
        if circuit:
            lines.append(
                f"Circuit: {circuit.get('individual', 0)} Individual, "
                f"{circuit.get('tribal', 0)} Tribal, "
                f"{circuit.get('collective', 0)} Collective "
                f"({circuit.get('dominant', 'none')} dominant)"
            )
        
        # Transit conditioning summary
        conditioned = conditioning.get("conditioned_gates", [])
        bridged = conditioning.get("bridged_channels", [])
        if conditioned:
            lines.append(f"Transit-conditioned gates: {conditioned}")
        if bridged:
            lines.append(f"Transit-bridged channels: {bridged}")
        
        # AstroHD gap
        p_only = astro.get("personality_only", [])
        d_only = astro.get("design_only", [])
        if p_only or d_only:
            lines.append(
                f"AstroHD: {len(astro.get('personality_gates', []))} P-gates "
                f"(how others experience), {len(astro.get('design_gates', []))} D-gates "
                f"(unconscious). Gap: P-only={p_only[:5]}, D-only={d_only[:5]}"
            )
        
        lines.append("")
        lines.append(
            "TRANSLATION RULES: Never output gate numbers, channel names, or HD jargon. "
            "Translate everything into real-world psychological equivalents. "
            "Frame everything as an experiment, not a diagnosis. "
            "Goal: help the user catch their own patterns, not depend on you to flag them."
        )
        
        context = "\n".join(lines)
        _hd_cache[profile] = (now, context)
        return context
        
    except Exception as e:
        logger.warning(f"Silent HD fetch failed: {e}")
        return None


# ── Journal System ───────────────────────────────────────────────
JOURNALS_DIR = Path(os.environ.get(
    "NEXTSTEP_JOURNALS_DIR",
    str(Path(__file__).parent / "journals")
))

def _get_recent_journal_context(user_id: int) -> str | None:
    """Read the last 7 days of journal entries, return as context."""
    try:
        now = datetime.now(timezone.utc)
        entries = []
        for days_ago in range(7):
            d = now - __import__('datetime').timedelta(days=days_ago)
            journal_path = JOURNALS_DIR / str(d.year) / f"{d.month:02d}" / f"{d.day:02d}.md"
            if journal_path.exists():
                with open(journal_path) as f:
                    entries.append(f.read().strip())
        if entries:
            return "[RECENT JOURNAL ENTRIES]\n" + "\n---\n".join(entries[-3:])
    except Exception as e:
        logger.warning(f"Journal read failed: {e}")
    return None


def _append_journal_entry(user_id: int, entry: str) -> None:
    """Append a journal entry for today. Creates directory structure as needed."""
    try:
        now = datetime.now(timezone.utc)
        day_dir = JOURNALS_DIR / str(now.year) / f"{now.month:02d}"
        day_dir.mkdir(parents=True, exist_ok=True)
        journal_path = day_dir / f"{now.day:02d}.md"
        
        timestamp = now.strftime("%H:%M UTC")
        with open(journal_path, "a") as f:
            f.write(f"\n### {timestamp}\n{entry}\n")
        
        logger.info(f"Journal entry written: {journal_path}")
    except Exception as e:
        logger.warning(f"Journal write failed: {e}")


# ── Birth Data Extraction ────────────────────────────────────────
def extract_birth_data(text: str) -> dict | None:
    """
    Extract birth date, time, and location from freeform text.
    Uses regex first, falls back to AI if pattern matching fails.
    Returns dict with year/month/day/hour/minute/location_str or None.
    """
    import re
    
    # Try regex extraction first
    result = _regex_extract_birth(text)
    if result:
        logger.info(f"Regex extracted birth data: {result}")
        return result
    
    # Fall back to AI extraction
    if client:
        result = _ai_extract_birth(text)
        if result:
            logger.info(f"AI extracted birth data: {result}")
            return result
    
    return None


def _regex_extract_birth(text: str) -> dict | None:
    """Regex-based birth data extractor."""
    import re
    
    # Date: MM/DD/YYYY, MM-DD-YYYY, YYYY-MM-DD, Month DD YYYY
    date_patterns = [
        r'(\d{1,2})[/\-\.](\d{1,2})[/\-\.](\d{2,4})',  # MM/DD/YYYY or DD/MM
        r'(\d{4})[/\-\.](\d{1,2})[/\-\.](\d{1,2})',      # YYYY-MM-DD
    ]
    month_names = r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*'
    month_name_pattern = rf'({month_names})[\s,]+(\d{{1,2}})[\s,]+(\d{{2,4}})'
    
    date_match = None
    date_format = None  # "mdy", "ymd", "monthname"
    
    for pat in date_patterns:
        m = re.search(pat, text)
        if m:
            g1, g2, g3 = int(m.group(1)), int(m.group(2)), int(m.group(3))
            date_match = (g1, g2, g3)
            # Heuristic: if first > 12, it's YYYY-MM-DD
            if g1 > 31:
                date_format = "ymd"
            else:
                date_format = "mdy"
            break
    
    if not date_match:
        m = re.search(month_name_pattern, text, re.IGNORECASE)
        if m:
            month_map = {m.lower()[:3]: i for i, m in enumerate(
                ['jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec'], 1)}
            mon = month_map.get(m.group(1).lower()[:3], 1)
            # Group 2 is inner month capture, group 3 is day, group 4 is year
            # due to nested (Jan|Feb|...) inside month_names group
            day = int(m.group(3))
            year = int(m.group(4))
            date_match = (mon, day, year)
            date_format = "monthname"
    
    if not date_match:
        return None
    
    # Time: @HH:MM, HH:MM AM/PM, @HH [AM/PM]
    time_match = re.search(
        r'@\s*(\d{1,2})(?::(\d{2}))?\s*([AaPp][Mm])?'
        r'|\b(\d{1,2}):(\d{2})\s*([AaPp][Mm])?\b',
        text)
    
    if not time_match:
        return None
    
    # Extract time components
    if time_match.group(1) is not None:  # @ format
        hour = int(time_match.group(1))
        minute = int(time_match.group(2) or 0)
        ampm = (time_match.group(3) or '').upper()
    else:  # HH:MM format
        hour = int(time_match.group(4))
        minute = int(time_match.group(5))
        ampm = (time_match.group(6) or '').upper()
    
    # Adjust for AM/PM
    if ampm == 'PM' and hour != 12:
        hour += 12
    elif ampm == 'AM' and hour == 12:
        hour = 0
    
    # Location: extract city/state/coords after the time
    # Find everything after the time match
    time_end = time_match.end()
    location_raw = text[time_end:].strip().lstrip(',').lstrip('@').lstrip('in').strip()
    
    if not location_raw:
        return None
    
    # Resolve the date
    if date_format == "ymd":
        year, month, day = date_match
    elif date_format in ("mdy", "monthname"):
        month, day, year = date_match
    
    # Handle 2-digit years
    if year < 100:
        year += 1900 if year > 50 else 2000
    
    return {
        "year": year,
        "month": month,
        "day": day,
        "hour": hour + minute / 60.0,
        "location_str": location_raw,
    }


def _ai_extract_birth(text: str) -> dict | None:
    """Use DeepSeek to extract birth data from text."""
    prompt = f"""Extract birth data from the following message. Return ONLY a JSON object with no other text.

If birth data IS present:
{{
  "found": true,
  "year": 2000, "month": 1, "day": 1,
  "hour": 12.0,
  "location_str": "City, Country"
}}

If birth data is NOT present:
{{"found": false}}

Message: {text}"""
    
    try:
        response = client.chat.completions.create(
            model=PROVIDER_MODEL,
            messages=[{"role": "system", "content": prompt}],
            max_tokens=150,
            temperature=0.0,
        )
        result = json.loads(response.choices[0].message.content.strip())
        if result.get("found"):
            return result
    except Exception as e:
        logger.error(f"AI birth extraction error: {e}")
    return None


async def handle_birth_query(update: Update, text: str, user_id: int, name: str) -> None:
    """Handle a birth data message: compute and send the bodygraph chart."""
    from cosmic_calculator import calculate_natal_chart
    from image_generator import render_bodygraph
    from ephemeris_engine import init_ephemeris
    from geo_resolver import resolve_location, local_to_utc

    birth = extract_birth_data(text)
    if not birth:
        await update.message.reply_text(
            f"I think you're sharing birth data, but I couldn't parse it. "
            f"Try:\n• `12/10/1989 @17:07 Simi Valley, CA`\n• `March 5 1992, 2:30 PM in Austin TX`"
        )
        return

    await update.message.reply_text("🔮 Computing your Human Design chart... give me a moment!")

    try:
        init_ephemeris()

        loc = birth.get("location_str", "UTC")

        # Convert local birth time to UTC
        utc_year, utc_month, utc_day, utc_hour = local_to_utc(
            birth["year"], birth["month"], birth["day"],
            birth["hour"], loc
        )

        birth_dt = datetime(utc_year, utc_month, utc_day,
                           int(utc_hour), int((utc_hour % 1) * 60))

        # Resolve geo coordinates
        geo = resolve_location(loc)
        lat = geo.get("lat", 0.0)
        lon = geo.get("lon", 0.0)

        chart = calculate_natal_chart(
            name=name,
            birth_dt=birth_dt,
            lat=lat, lon=lon,
            timezone=geo.get("timezone", "UTC"),
        )

        output_path = f"/tmp/bodygraph_{user_id}.png"
        render_bodygraph(chart, output_path)

        with open(output_path, "rb") as f:
            channels_text = ', '.join(
                f"{c['gates'][0]}-{c['gates'][1]}" for c in chart.get('defined_channels', []))
            caption = (
                f"*{chart['hd_type']} | {chart['profile']} | {chart['authority']}*\n"
                f"_{chart['strategy']}_\n\n"
                f"Defined: {', '.join(chart.get('defined_centers', []))}\n"
                f"Channels: {channels_text}"
            )
            await update.message.reply_photo(photo=f, caption=caption)

    except ImportError as e:
        logger.exception(f"Birth chart import error: {e}")
        await update.message.reply_text(f"⚠️ Couldn't load the chart engine.\nError: {str(e)[:150]}")
    except Exception as e:
        logger.exception(f"Birth chart render error: {e}")
        await update.message.reply_text(f"⚠️ Chart rendering failed.\nError: {str(e)[:200]}")


# ── Relationship Handler ──────────────────────────────────────────
NAME_TO_KEY = {
    "becca": "becca", "rebecca": "becca",
    "benjamin": "benjamin", "ben": "benjamin",
    "william": "william", "will": "william",
    "victoria": "victoria", "tori": "victoria", "v": "victoria",
    "michael": "michael", "mike": "michael",
}

def _find_relationship_target(text: str, self_name: str) -> str | None:
    """Find which family member is being referenced in text."""
    text_lower = text.lower()
    for name, key in NAME_TO_KEY.items():
        if name in text_lower and key != _active_profile:
            return key
    # Heuristic: "my wife" → becca
    if any(phrase in text_lower for phrase in ["my wife", "wife", "becca"]):
        return "becca"
    return None


async def handle_relationship_query(update: Update, text: str, self_name: str):
    """Handle relationship questions by computing synastry composite."""
    target = _find_relationship_target(text, self_name)

    if not target:
        # Try to extract using DeepSeek
        family_list = ", ".join(f"{k} ({v['name']})" for k, v in _family_data.items())
        prompt = f"Which family member is being referenced in this message? Family: {family_list}. Return just the profile key (e.g. 'becca') or 'none'.\nMessage: {text}"
        try:
            response = client.chat.completions.create(
                model=PROVIDER_MODEL,
                messages=[{"role": "system", "content": prompt}],
                max_tokens=20, temperature=0.0,
            )
            ai_target = response.choices[0].message.content.strip().lower()
            if ai_target in _family_data and ai_target != _active_profile:
                target = ai_target
        except Exception:
            pass

    if not target:
        await update.message.reply_text(
            f"🤔 I'm not sure which family member you're asking about.\n"
            f"Try `/relate becca` or just say \"me and Becca\"."
        )
        return

    target_name = _family_data.get(target, {}).get("name", target)
    await update.message.reply_text(f"🔍 Analyzing {self_name} + {target_name} composite...")

    try:
        _ensure_mcp_path()
        from mcp_server import get_relationship_composite
        from ephemeris_engine import init_ephemeris

        init_ephemeris()
        result = get_relationship_composite(_active_profile, target)

        if "error" in result:
            await update.message.reply_text(f"⚠️ {result['error']}")
            return

        comp = result["composite"]
        companion = result.get("companion_channels", [])
        electro = comp.get("electromagnetic_channels", [])
        dominance = comp.get("dominance_channels", [])
        centers = comp.get("centers_defined_together", [])

        lines = [f"*{self_name} + {target_name} — Relationship Composite*\n"]

        if companion:
            names = ", ".join(f"{c['gates'][0]}-{c['gates'][1]} {c['name']}" for c in companion)
            lines.append(f"🤝 *Companion:* {names}")
            lines.append("_You both have this channel fully defined. Deep resonance._\n")

        if electro:
            lines.append(f"⚡ *Electromagnetic ({len(electro)} channels):*")
            for e in electro:
                lines.append(f"• {e['gates'][0]}-{e['gates'][1]} *{e['name']}*")
                lines.append(f"  _{self_name} has Gate {e['a_has']}, {target_name} has Gate {e['b_has']}_")
            lines.append("")

        if dominance:
            lines.append(f"🔄 *Dominance ({len(dominance)} channels):*")
            for d in dominance:
                lines.append(f"• {d['gates'][0]}-{d['gates'][1]} *{d['name']}* ({d['type']})")
            lines.append("")

        lines.append(f"🏠 *Centers defined together:* {', '.join(centers)} ({len(centers)}/9)")
        lines.append(f"🔗 *Shared gates:* {result.get('shared_gate_count', 0)}")

        await update.message.reply_text("\n".join(lines))

    except Exception as e:
        logger.exception(f"Relationship error: {e}")
        await update.message.reply_text(f"⚠️ Relationship analysis failed.\nError: {str(e)[:200]}")


# ── Image Commands ────────────────────────────────────────────────
async def chart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate and send a bodygraph chart image."""
    user = update.effective_user
    name = user.first_name
    await update.message.reply_text("🔮 Generating your bodygraph... give me a moment!")

    try:
        from cosmic_calculator import calculate_natal_chart
        from image_generator import render_bodygraph
        from ephemeris_engine import init_ephemeris
        from geo_resolver import local_to_utc

        init_ephemeris()

        b = _get_active_birth()
        # Convert LOCAL birth time to UTC (calculate_natal_chart expects UTC)
        utc_year, utc_month, utc_day, utc_hour = local_to_utc(
            b["year"], b["month"], b["day"], b["hour"],
            b["location"]
        )
        birth_dt = datetime(utc_year, utc_month, utc_day,
                           int(utc_hour), int((utc_hour % 1) * 60))
        
        chart = calculate_natal_chart(
            name=b["name"], birth_dt=birth_dt,
            lat=b["lat"], lon=b["lon"], timezone="America/Los_Angeles",
        )

        output_path = f"/tmp/bodygraph_{user.id}.png"
        render_bodygraph(chart, output_path)
        
        with open(output_path, "rb") as f:
            channels_text = ', '.join(f"{c['gates'][0]}-{c['gates'][1]}" for c in chart['defined_channels'])
            caption = (
                f"*{chart['hd_type']} | {chart['profile']} | {chart['authority']}*\n"
                f"_{chart['strategy']}_\n\n"
                f"Defined: {', '.join(chart['defined_centers'])}\n"
                f"Channels: {channels_text}"
            )
            await update.message.reply_photo(photo=f, caption=caption)
    except ImportError as e:
        logger.exception(f"Chart import error: {e}")
        await update.message.reply_text(f"⚠️ Couldn't load the chart engine.\nError: {str(e)[:150]}")
    except Exception as e:
        logger.exception(f"Chart render error: {e}")
        await update.message.reply_text(f"⚠️ Chart rendering failed.\nError: {str(e)[:200]}")


async def map_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate and send an astrocartography world map."""
    await update.message.reply_text("🗺️ Generating your astrocartography map... this takes a few seconds!")

    try:
        from astro_cartography import calculate_cartography_lines
        from image_generator import render_cartography_map
        from ephemeris_engine import init_ephemeris, julday
        from geo_resolver import local_to_utc

        init_ephemeris()

        b = _get_active_birth()
        # Convert LOCAL birth time to UTC before computing Julian Day
        utc_year, utc_month, utc_day, utc_hour = local_to_utc(
            b["year"], b["month"], b["day"], b["hour"],
            b["location"]
        )
        jd = julday(utc_year, utc_month, utc_day, utc_hour)
        lines = calculate_cartography_lines(jd)

        output_path = f"/tmp/cartography_{update.effective_user.id}.png"
        render_cartography_map(lines, output_path, title="Michael's Astrocartography")

        with open(output_path, "rb") as f:
            await update.message.reply_photo(
                photo=f,
                caption="🗺️ *Your Astrocartography Map*\n"
                        "Lines show where each planet sits on the 4 major angles.\n"
                        "_ASC= Rising, DSC=Setting, MC=Culminating, IC=Nadir_"
            )
    except ImportError as e:
        logger.exception(f"Map import error: {e}")
        await update.message.reply_text(f"⚠️ Couldn't load the mapping engine.\nError: {str(e)[:150]}")
    except Exception as e:
        logger.exception(f"Map render error: {e}")
        await update.message.reply_text(f"⚠️ Map rendering failed.\nError: {str(e)[:200]}")


async def where_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recommend best cities for career/love/family based on astrocartography."""
    args = context.args
    category = args[0].lower() if args else "general"
    valid = {"career", "love", "family", "creativity", "general"}
    if category not in valid:
        category = "general"

    b = _get_active_birth()
    await update.message.reply_text(f"🗺️ Scanning 95 cities for {b['name']}'s best {category} locations...")

    try:
        from location_scorer import rank_cities
        from ephemeris_engine import init_ephemeris, julday
        from geo_resolver import local_to_utc

        init_ephemeris()
        utc = local_to_utc(b["year"], b["month"], b["day"], b["hour"], b["location"])
        jd = julday(utc[0], utc[1], utc[2], utc[3])

        results = rank_cities(jd, category=category, top_n=10)

        lines = [f"*Top {category.title()} Locations for {b['name']}:*\n"]
        for i, r in enumerate(results, 1):
            city = r["city"]
            country = r["country"]
            score = r["normalized"][category]
            top_p = r.get("top_planets", [])
            planet_str = ", ".join(f"{p['planet']} {p['angle']}" for p in top_p[:3])
            lines.append(f"{i}. *{city}, {country}* — {score:.1f}")
            if planet_str:
                lines.append(f"   _{planet_str}_")

        await update.message.reply_text("\n".join(lines))
    except ImportError as e:
        logger.exception(f"Where import error: {e}")
        await update.message.reply_text(f"⚠️ Location scorer not available.\nError: {str(e)[:150]}")
    except Exception as e:
        logger.exception(f"Where error: {e}")
        await update.message.reply_text(f"⚠️ Location scan failed.\nError: {str(e)[:200]}")


async def who_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List family members or switch active profile."""
    args = context.args
    _load_family()

    if not args:
        # List all members
        lines = ["*Family Profiles:*\n"]
        for key, member in _family_data.items():
            marker = "👉" if key == _active_profile else "  "
            lines.append(f"{marker} `{key}` — {member['name']} ({member.get('hd_type','?')} {member.get('profile','?')})")
        lines.append(f"\nType `/who NAME` to switch. Active: *{_active_profile}*")
        await update.message.reply_text("\n".join(lines))
        return

    profile = args[0].lower()
    if _set_active_profile(profile):
        member = _family_data[profile]
        await update.message.reply_text(
            f"✅ Switched to *{member['name']}*\n"
            f"{member.get('hd_type','?')} | {member.get('profile','?')} | {member.get('authority','?')}\n\n"
            f"/chart /map /where now use {member['name']}'s data."
        )
    else:
        available = ", ".join(f"`{k}`" for k in _family_data.keys())
        await update.message.reply_text(f"Unknown profile. Available: {available}")


async def relate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show relationship composite between active profile and target."""
    args = context.args
    _load_family()
    active_name = _family_data.get(_active_profile, {}).get("name", _active_profile)

    if not args:
        available = ", ".join(f"`{k}`" for k in _family_data.keys() if k != _active_profile)
        await update.message.reply_text(f"Usage: `/relate NAME`\nAvailable: {available}")
        return

    target = args[0].lower()
    if target not in _family_data:
        await update.message.reply_text(f"Unknown profile '{target}'. Use /who to see family.")
        return
    if target == _active_profile:
        await update.message.reply_text("That's you! Try `/relate becca` or another family member.")
        return

    # Reuse relationship query handler
    fake_update = update
    await handle_relationship_query(fake_update, f"me and {target}", active_name)


# ── Main ─────────────────────────────────────────────────────────
def main():
    if not PROVIDER_API_KEY:
        logger.warning("NEXTSTEP_API_KEY or DEEPSEEK_API_KEY not set. Running in fallback mode (no AI).")
    
    logger.info(f"Starting Next Step bot as {ASSISTANT_NAME} ({INSTANCE_PROFILE})...")
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("list", lambda u, c: handle_message(u, c)))
    app.add_handler(CommandHandler("status", lambda u, c: handle_message(u, c)))
    app.add_handler(CommandHandler("chart", chart_cmd))
    app.add_handler(CommandHandler("map", map_cmd))
    app.add_handler(CommandHandler("where", where_cmd))
    app.add_handler(CommandHandler("who", who_cmd))
    app.add_handler(CommandHandler("relate", relate_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    logger.info(f"{ASSISTANT_NAME} ({INSTANCE_PROFILE}) is running! Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
