#!/bin/bash
# Next Step Bot Launcher
# Sources DeepSeek API key from orchestrator profile and starts the bot

# Source the orchestrator profile's .env for API keys
set -a
source /home/ubuntu/.hermes/profiles/orchestrator/.env 2>/dev/null
set +a

# Use the hermes-agent venv Python (has python-telegram-bot + openai)
exec /home/ubuntu/.local/share/pipx/venvs/hermes-agent/bin/python \
    /home/ubuntu/work/next-step-bot/bot.py
