# Next Step Bot — Executive Function Assistant for AuDHD

A Telegram bot that helps neurodivergent humans stay on track by hiding the mountain and serving exactly ONE task at a time.

**Bot:** [@TheNextNextStepBot](https://t.me/TheNextNextStepBot)  
**Assistant Name:** Fred  
**Built for:** Michael Gulden (3/5 Projector, Splenic Authority)  
**Powered by:** DeepSeek API

## How It Works

1. Dump your chaotic thoughts — Fred organizes everything behind the scenes
2. Fred gives you exactly ONE atomic task at a time
3. Say "done" — Fred celebrates and gives you the next step
4. The full list stays hidden unless you explicitly ask

## Commands

| Command | What it does |
|---|---|
| `/start` | Welcome message + instructions |
| `/help` | List all commands |
| `done` or ✅ | Complete current task → 🎉 dopamine party → next step |
| `/next` | Skip to next task |
| `/add [tasks]` | Add new tasks (chaos accepted!) |
| `/list` | Show all pending tasks (opt-in only) |
| `/status` | See what you're working on |

## Features

- **Hide the Mountain** — Never shows full list unless asked
- **Micro-Scoping** — Breaks large tasks into stupidly small steps
- **Dopamine Party** — Variable celebrations on completion (never repeats)
- **Bottom-Up Friendly** — Every task is a concrete brick, never a vague building
- **Persistence** — Tasks survive restarts (SQLite)
- **3/5-Aware Coaching** — Michael's Human Design profile informs tone + framing

## Installation

### Prerequisites
- Python 3.12+ with `python-telegram-bot`, `openai`
- DeepSeek API key in environment: `DEEPSEEK_API_KEY`

### Quick Start
```bash
# Source API key
source /home/ubuntu/.hermes/profiles/orchestrator/.env

# Run
/home/ubuntu/.local/share/pipx/venvs/hermes-agent/bin/python bot.py
```

### Systemd Service
```bash
sudo cp next-step-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now next-step-bot
```

## Architecture

```
Telegram ↔ bot.py (python-telegram-bot) ↔ DeepSeek API (deepseek-chat)
                                          ↕
                                     SQLite (next_step.db)
```

- `bot.py` — Main bot, all command handlers, message routing
- `next_step.db` — Task queue + celebration rotation state
- `soul.md` — System prompt / personality definition (see Hermes profile)

## Future (Phase 2)
- [ ] Voice note ingestion via Whisper
- [ ] Human Design MCP integration for dynamic coaching
- [ ] Becca's 6/2 profile (separate instance)
- [ ] Linear/GitHub integration for passive context
- [ ] Web dashboard

## Files
- `bot.py` — Main application
- `run.sh` — Launch script
- `next-step-bot.service` — systemd unit
- `../hermes/profiles/next-step/SOUL.md` — Personality definition
