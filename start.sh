#!/bin/bash
set -e
echo "🚀 Starting literallybot (dev) ..."
echo "📍 Working directory: $(pwd)"
if [ ! -f .env ]; then
  echo "❌ .env missing. Create with: DISCORD_TOKEN=..."; exit 1; fi
if ! python3 -c "import discord" 2>/dev/null; then
  echo "📦 Installing dependencies..."; pip install -r requirements.txt; fi
python3 bot.py
