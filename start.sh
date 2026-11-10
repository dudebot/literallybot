#!/bin/sh
# Start the bot: build the venv if it isn't there, install requirements
# when they've changed, run the bot. The bot itself handles the token — it
# reads $DISCORD_TOKEN, else its saved config, else prompts you for one.
#
# Kept deliberately dumb and readable: anyone should be able to see exactly
# what this does to their machine before running it.
set -e
cd "$(dirname "$0")"

PYTHON=python3
command -v $PYTHON >/dev/null 2>&1 || PYTHON=python
command -v $PYTHON >/dev/null 2>&1 || {
  echo "Python 3 not found. Install it from https://python.org and try again." >&2
  exit 1
}
$PYTHON -c 'import sys; sys.exit(sys.version_info < (3, 11))' || {
  echo "Python 3.11 or newer is required; $PYTHON is $($PYTHON -V 2>&1)." >&2
  exit 1
}

if ! venv/bin/python -c '' >/dev/null 2>&1; then
  echo "Creating virtual environment..."
  rm -rf venv
  $PYTHON -m venv venv || {
    rm -rf venv
    echo "Could not create the virtual environment (on Debian/Ubuntu: apt install python3-venv)." >&2
    exit 1
  }
fi

# Reinstall only when requirements.txt is newer than the last successful
# install, so a normal start doesn't wait on pip.
STAMP=venv/.requirements-stamp
if [ ! -f "$STAMP" ] || [ requirements.txt -nt "$STAMP" ]; then
  echo "Installing dependencies..."
  venv/bin/python -m pip install --quiet --upgrade pip
  venv/bin/python -m pip install --quiet -r requirements.txt
  touch "$STAMP"
fi

exec venv/bin/python bot.py
