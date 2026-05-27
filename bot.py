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

# Default birth data (Michael)
DEFAULT_BIRTH = {
    "name": "Michael",
    "year": 1989, "month": 12, "day": 10, "hour": 17.1167,  # 5:07 PM
    "location": "Simi Valley CA",
    "lat": 34.2694, "lon": -118.7815,
}

# ── Config ──────────────────────────────────────────────────────
BOT_TOKEN = "***REDACTED***"
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
  "type": "task_dump" | "done" | "command" | "chatter",
  "reasoning": "brief one-line explanation"
}

Rules:
- "task_dump": User is listing things they need to do. Even if messy/chaotic. Multi-line, comma-separated, bullet lists, stream-of-consciousness brain dumps.
- "done": User is signaling completion of current task. Includes: "done", "✅", "finished", "complete", "did it", "that's done", "all done", etc.
- "command": User is asking for status/list/help or using a command like /list, /next, /add, /status, /help, /start.
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
    # If it has multiple lines, commas, or bullet points, treat as task dump
    if "\n" in t or "," in t or t.startswith("-") or t.startswith("*"):
        return "task_dump"
    return "chatter"


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

        init_ephemeris()

        b = DEFAULT_BIRTH
        birth_dt = datetime(b["year"], b["month"], b["day"],
                           int(b["hour"]), int((b["hour"] % 1) * 60))
        
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

        init_ephemeris()

        b = DEFAULT_BIRTH
        jd = julday(b["year"], b["month"], b["day"], b["hour"])
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    logger.info(f"{ASSISTANT_NAME} is running! Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
