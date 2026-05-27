#!/usr/bin/env python3
"""
Next Step — Executive Function Telegram Bot for AuDHD
Built for Michael (3/5 Projector, Splenic Authority)
Assistant: Jamie | Powered by DeepSeek API

ARCHITECTURE:
  Every user message → DeepSeek classifier → 
    "task_dump" → parse + micro-scope → save ONE atomic step → serve
    "done" → mark complete → celebrate → serve next
    "command" → handle directly
    "chatter" → respond conversationally
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

# MCP import path
MCP_SRC = "/home/ubuntu/work/OpenHumanDesignMCP/hd-mcp-server/src"
sys.path.insert(0, MCP_SRC)

# Family data — loaded from family.json, falls back to Michael
FAMILY_PATH = Path(__file__).parent / "family.json"
_family_data = {}
_active_profile = "michael"

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

# Active birth data — call this whenever you need the current profile
DEFAULT_BIRTH = _get_active_birth()  # initial load, also use _get_active_birth() dynamically

# ── Config ──────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
if not BOT_TOKEN:
    # Legacy fallback — will be removed after token rotation
    BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
DB_PATH = Path(__file__).parent / "next_step.db"
ASSISTANT_NAME = "Jamie"

# DeepSeek API
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL) if DEEPSEEK_API_KEY else None

# ── Logging ─────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    stream=sys.stderr,
)
logger = logging.getLogger("next-step")

# ── System Prompts ──────────────────────────────────────────────
CLASSIFIER_PROMPT = """You are a message classifier for an AuDHD executive function assistant bot named Jamie. Analyze the user's message and classify it as exactly ONE type.

Return ONLY a JSON object with no other text:
{
  "type": "task_dump" | "done" | "command" | "chatter" | "birth_query",
  "reasoning": "brief one-line explanation"
}

Rules:
- "task_dump": User is listing things they need to do. Even if messy/chaotic. Multi-line, comma-separated, bullet lists, stream-of-consciousness brain dumps.
- "done": User is signaling completion of current task. Includes: "done", "✅", "finished", "complete", "did it", "that's done", "all done", etc.
- "command": User is asking for status/list/help or using a command like /list, /next, /add, /status, /help, /start.
- "birth_query": User is providing birth data — a date (with digits or month names), a time (digits + colon or @ or AM/PM), and a location (city/state/coordinates). Examples: "12/10/1989 @17:07 Simi Valley, CA", "March 5 1992, 2:30 PM in Austin TX", "1999-06-15 08:45 London UK". Route this to chart generation — do NOT treat as task_dump or chatter.
- "chatter": Everything else — greetings, questions, meta-commentary, "hey Jamie", "thanks", etc.

User name: {name}
Message: {message}"""

PARSE_PROMPT = """You are Jamie, an executive function assistant for {name}, who has an AuDHD brain and is a 3/5 Projector (learns by experimenting, needs concrete steps).

{name} just dumped their chaotic thoughts. Your job:
1. Strip out ALL conversational filler, meta-dialogue, greetings, "hey Jamie", etc.
2. Parse the remaining content into distinct individual tasks.
3. For the FIRST task, micro-scope it: if it's vague ("fix auth bug"), break it into a stupidly small mechanical first step ("Open the auth module file at src/auth.py").

Return ONLY a JSON object with no other text:
{{
  "parsed_tasks": ["task 1", "task 2", "task 3"],
  "served_step": "The exact atomic next step to show the user. Must be concrete and mechanical.",
  "greeting": "Brief, encouraging acknowledgment (1 sentence max)."
}}

Rules:
- NEVER show the full task list in served_step.
- Every served_step must be ONE concrete action. "Open X file" not "Fix X bug".
- Be warm and playful but concise.
- {name} is a bottom-up thinker. Give mechanical bricks, never vague buildings."""

MICROSCOPE_PROMPT = """You are Jamie, an executive function assistant. The next task in the queue is:

TASK: {task}

If this task is already specific and atomic (like "Open the file at src/auth.py"), return it as-is.
If it's vague or large, break it into a stupidly small, mechanical first step.

Return ONLY a JSON object:
{{
  "served_step": "The exact atomic next step. One concrete action.",
  "was_scoped": true if you had to break it down, false if it was already atomic
}}

Rules:
- ONE concrete action only. "Open the terminal" or "Navigate to src/" — not both.
- Mechanical language. Action verbs. File paths when relevant.
- Bottom-up friendly. No vague goals."""

DONE_RESPONSE_PROMPT = """You are Jamie, an executive function assistant for {name} (AuDHD, 3/5 Projector). They just completed a task.

1. Give a short, high-energy celebration (1 sentence). Make it novel — never the same style twice.
2. Tell them what's next (the step_provided below).

Return ONLY a JSON object:
{{
  "celebration": "Your novel celebration text with emoji",
  "transition": "Brief handoff to the next step (1 sentence max)"
}}

Current next step: {next_step}

Celebration style guide: Use emoji, be playful, vary between:
- Video game references ("🎉 PEW PEW! Task obliterated!")
- Sports metaphors ("🏆 Another win for the highlight reel!")
- Superhero framing ("🦾 Task eliminated. You're unstoppable!")
- Cheerful absurdity ("💥 BOOM. That task never stood a chance!")
NEVER repeat the same style twice. Be creative."""

CHATTER_RESPONSE_PROMPT = """You are Jamie, an executive function assistant for {name} (AuDHD, 3/5 Projector, Splenic authority). They just said something conversational.

Respond warmly and concisely (1-2 sentences). If appropriate, gently remind them of their current task or encourage them to dump what's on their mind.

Message: {message}
Current task: {current_task}"""

# ── Database ─────────────────────────────────────────────────────
def get_db():
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
    conn.commit()
    return conn


# ── AI Classifier ────────────────────────────────────────────────
def classify_message(text: str, name: str) -> str:
    """Classify user message as task_dump, done, command, or chatter."""
    if not client:
        return fallback_classify(text)
    
    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{
                "role": "system",
                "content": CLASSIFIER_PROMPT.format(name=name, message=text)
            }],
            max_tokens=100,
            temperature=0.0,
        )
        result = json.loads(response.choices[0].message.content.strip())
        return result.get("type", "chatter")
    except Exception as e:
        logger.error(f"Classifier error: {e}")
        return fallback_classify(text)


def fallback_classify(text: str) -> str:
    """Fallback classifier when AI is unavailable."""
    t = text.lower().strip()
    done_words = ("done", "✅", "✔️", "finished", "complete", "did it", "finished it")
    if t in done_words or any(t.startswith(w) for w in done_words):
        return "done"
    if t.startswith("/"):
        return "command"
    # Check for birth data: date + time + location pattern
    if _looks_like_birth_data(text):
        return "birth_query"
    # If it has multiple lines, commas, or bullet points, treat as task dump
    if "\n" in t or "," in t or t.startswith("-") or t.startswith("*"):
        return "task_dump"
    return "chatter"


def _looks_like_birth_data(text: str) -> bool:
    """Quick heuristic: does this text contain date, time, and location indicators?"""
    import re
    # Date pattern: digits with separators or month names
    has_date = bool(re.search(
        r'\b\d{1,2}[/\-\.]\d{1,2}[/\-\.](?:\d{2}|\d{4})\b'
        r'|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4}\b',
        text, re.IGNORECASE))
    # Time pattern: digits with colon or @ or AM/PM
    has_time = bool(re.search(
        r'@\s*\d{1,2}(?::\d{2})?\s*(?:[AaPp][Mm])?'
        r'|\b\d{1,2}:\d{2}\s*(?:[AaPp][Mm])?\b',
        text))
    # Location: contains a recognizable city/state/coord pattern
    has_location = bool(re.search(
        r'[A-Z][a-z]+(?:[, ]+\s*[A-Z]{2}\b|[A-Z][a-z]+)'
        r'|latitude|longitude'
        r'|\b\d+[°]\s*\d+[\']\s*[NSEW]',
        text))
    return has_date and has_time


# ── AI Parsing ──────────────────────────────────────────────────
def parse_task_dump(text: str, name: str) -> dict:
    """Parse a chaotic task dump into individual tasks + first micro-step."""
    if not client:
        return fallback_parse(text, name)
    
    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{
                "role": "system",
                "content": PARSE_PROMPT.format(name=name)
            }, {
                "role": "user",
                "content": text
            }],
            max_tokens=400,
            temperature=0.3,
        )
        result = json.loads(response.choices[0].message.content.strip())
        return result
    except Exception as e:
        logger.error(f"Parse error: {e}")
        return fallback_parse(text, name)


def fallback_parse(text: str, name: str) -> dict:
    """Fallback parser when AI unavailable."""
    lines = []
    for line in text.split("\n"):
        stripped = line.strip().lstrip("-*•0123456789. ")
        if stripped and len(stripped) > 3:
            lines.append(stripped)
    if not lines:
        return {"parsed_tasks": [], "served_step": f"Hey {name}! I didn't catch any tasks. Try listing them out?", "greeting": ""}
    return {
        "parsed_tasks": lines,
        "served_step": f"Got it. Your first step: {lines[0]}",
        "greeting": f"Got it, {name}."
    }


def micro_scope_task(task: str) -> dict:
    """Break a vague task into an atomic micro-step."""
    if not client:
        return {"served_step": task, "was_scoped": False}
    
    # Quick check: if task is already specific/short, don't scope
    if len(task.split()) <= 6:
        return {"served_step": task, "was_scoped": False}
    
    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{
                "role": "system",
                "content": MICROSCOPE_PROMPT.format(task=task)
            }],
            max_tokens=150,
            temperature=0.3,
        )
        result = json.loads(response.choices[0].message.content.strip())
        return result
    except Exception as e:
        logger.error(f"Micro-scope error: {e}")
        return {"served_step": task, "was_scoped": False}


def generate_done_response(name: str, next_step: str) -> dict:
    """Generate celebration + transition for task completion."""
    if not client:
        return {
            "celebration": "✅ Done! Great work.",
            "transition": f"Next up: {next_step}" if next_step else "Queue is empty! 🎉"
        }
    
    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{
                "role": "system",
                "content": DONE_RESPONSE_PROMPT.format(name=name, next_step=next_step or "Nothing — queue is empty!")
            }],
            max_tokens=100,
            temperature=0.9,
        )
        result = json.loads(response.choices[0].message.content.strip())
        return result
    except Exception as e:
        logger.error(f"Done response error: {e}")
        return {
            "celebration": "✅ Done!",
            "transition": f"Next up: {next_step}" if next_step else "Queue is empty! 🎉"
        }


def generate_chatter_response(name: str, message: str, current_task: str = None) -> str:
    """Generate a conversational response."""
    if not client:
        return f"Hey {name}! 👋 Ready to tackle some tasks? Just dump what's on your mind."
    
    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{
                "role": "system",
                "content": CHATTER_RESPONSE_PROMPT.format(
                    name=name, message=message,
                    current_task=current_task or "No current task"
                )
            }],
            max_tokens=80,
            temperature=0.7,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Chatter error: {e}")
        return f"Hey {name}! Ready to tackle some tasks?"


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


# ── Telegram Handlers ────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = user.first_name
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO state (user_id, user_name) VALUES (?, ?)", (user.id, name))
    conn.commit()
    conn.close()
    
    await update.message.reply_text(
        f"Hey {name}! 👋 I'm **{ASSISTANT_NAME}**, your Next Step assistant.\n\n"
        "Here's how it works:\n"
        "• Dump your tasks, thoughts, chaos — I'll organize it all\n"
        "• I'll give you exactly ONE thing to do at a time\n"
        "• Say **done** and I'll celebrate + give you the next step\n"
        "• I'll NEVER show you the full list (unless you ask)\n\n"
        "Ready? Send me what's on your mind! 🚀"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"**{ASSISTANT_NAME} — Your Next Step Assistant**\n\n"
        "**Commands:**\n"
        "/start — Start fresh\n"
        "/help — This message\n"
        "/list — See all pending tasks\n"
        "/status — See current task\n\n"
        "**Quick actions:**\n"
        "Just say 'done' or ✅ to complete\n"
        "Dump anything and I'll organize it\n"
        "I parse EVERYTHING through AI for smart task extraction!"
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
                f"⚠️ Jamie hit a snag processing that.\nError: {str(e)[:200]}\n\nTry again or type /start to reset."
            )
        except Exception:
            logger.error("Could not send error reply to user")


async def _handle_message_impl(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                text: str, user_id: int, name: str):
    conn = get_db()
    current_id, stored_name = get_or_create_state(conn, user_id)
    display_name = stored_name or name
    
    # ── STEP 1: Classify the message ──
    msg_type = classify_message(text, display_name)
    logger.info(f"[{display_name}] Classified as: {msg_type} | Text: {text[:80]}")

    # ── HANDLE: "done" ──
    if msg_type == "done":
        next_task = complete_current(conn, user_id)
        
        if next_task:
            # Micro-scope the next task
            scoped = micro_scope_task(next_task[1])
            served = scoped.get("served_step", next_task[1])
            
            # Update the task's micro_step_current
            conn.execute("UPDATE tasks SET micro_step_current = ? WHERE id = ?", (served, next_task[0]))
            conn.commit()
            
            # Generate celebration
            done_resp = generate_done_response(display_name, served)
            msg = f"{done_resp['celebration']}\n\n{done_resp['transition']}"
        else:
            done_resp = generate_done_response(display_name, None)
            msg = f"{done_resp['celebration']}\n\n{done_resp['transition']}"
        
        conn.close()
        await update.message.reply_text(msg)
        return
    
    # ── HANDLE: /list ──
    if msg_type == "command" and ("/list" in text.lower() or "list" in text.lower()):
        tasks = get_all_pending(conn, user_id)
        conn.close()
        if not tasks:
            await update.message.reply_text("Queue is empty! 🎉 Dump what's on your mind!")
        else:
            task_list = "\n".join(f"{i+1}. {t[1]}" for i, t in enumerate(tasks))
            await update.message.reply_text(f"**Your pending tasks:**\n\n{task_list}\n\nSay 'done' to complete the current one!")
        return
    
    # ── HANDLE: /status ──
    if msg_type == "command" and ("/status" in text.lower() or "status" in text.lower()):
        current_id, _ = get_or_create_state(conn, user_id)
        conn.close()
        if current_id:
            cur = conn.execute("SELECT description, micro_step_current FROM tasks WHERE id = ?", (current_id,))
            row = cur.fetchone()
            if row:
                step = row[1] or row[0]
                await update.message.reply_text(f"You're working on: **{step}**\n\nReply 'done' when complete!")
            else:
                await update.message.reply_text("No current task. Dump some tasks to begin!")
        else:
            await update.message.reply_text("No current task. Dump some tasks to begin!")
        return
    
    # ── HANDLE: /help or other commands ──
    if msg_type == "command":
        conn.close()
        await help_cmd(update, context)
        return
    
    # ── HANDLE: task_dump ──
    if msg_type == "task_dump":
        # Parse through DeepSeek
        parsed = parse_task_dump(text, display_name)
        tasks = parsed.get("parsed_tasks", [])
        served_step = parsed.get("served_step", "")
        greeting = parsed.get("greeting", "")
        
        if not tasks:
            conn.close()
            await update.message.reply_text(f"Hey {display_name}! I didn't catch any tasks in that. Try listing them out clearly?")
            return
        
        # Save raw dump
        dump_id = save_raw_dump(conn, user_id, text, len(tasks))
        
        # Add parsed tasks
        first_id, first_desc = add_tasks(conn, user_id, tasks, dump_id)
        
        if first_id:
            # Micro-scope the first task
            scoped = micro_scope_task(first_desc)
            served = scoped.get("served_step", served_step or first_desc)
            
            # Save the micro-step
            conn.execute("UPDATE tasks SET micro_step_current = ? WHERE id = ?", (served, first_id))
            
            # Set as current task
            conn.execute("UPDATE state SET current_task_id = ? WHERE user_id = ?", (first_id, user_id))
            conn.commit()
            
            await update.message.reply_text(f"{greeting} {served}")
        else:
            conn.commit()
            await update.message.reply_text(f"Got it, {display_name}! But I couldn't extract tasks. Try again?")
        
        conn.close()
        return
    
    # ── HANDLE: birth_query ──
    if msg_type == "birth_query":
        conn.close()
        await handle_birth_query(update, text, user_id, display_name)
        return
    
    # ── HANDLE: chatter ──
    if msg_type == "chatter":
        current_task_desc = None
        if current_id:
            cur = conn.execute("SELECT micro_step_current, description FROM tasks WHERE id = ?", (current_id,))
            row = cur.fetchone()
            if row:
                current_task_desc = row[0] or row[1]
        
        response = generate_chatter_response(display_name, text, current_task_desc)
        conn.close()
        await update.message.reply_text(response)
        return
    
    conn.close()


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
  "year": 1989, "month": 12, "day": 10,
  "hour": 17.1167,
  "location_str": "Simi Valley, CA"
}}

If birth data is NOT present:
{{"found": false}}

Message: {text}"""
    
    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
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


# ── Main ─────────────────────────────────────────────────────────
def main():
    if not DEEPSEEK_API_KEY:
        logger.warning("DEEPSEEK_API_KEY not set. Running in fallback mode (no AI).")
    
    logger.info(f"Starting Next Step bot as {ASSISTANT_NAME}...")
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("list", lambda u, c: handle_message(u, c)))
    app.add_handler(CommandHandler("status", lambda u, c: handle_message(u, c)))
    app.add_handler(CommandHandler("chart", chart_cmd))
    app.add_handler(CommandHandler("map", map_cmd))
    app.add_handler(CommandHandler("where", where_cmd))
    app.add_handler(CommandHandler("who", who_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    logger.info(f"{ASSISTANT_NAME} is running! Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
