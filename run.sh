#!/bin/sh
# sets up a venv the first time, then starts the server.
# LARDER_HOST / LARDER_PORT to change where it listens, LARDER_DATA to move the data folder.
set -e
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
    echo "first run, making .venv and installing requirements..."
    python3 -m venv .venv
    .venv/bin/pip install -q --upgrade pip
    .venv/bin/pip install -q -r requirements.txt
elif [ requirements.txt -nt .venv/.installed ]; then
    .venv/bin/pip install -q -r requirements.txt
fi
touch .venv/.installed

exec .venv/bin/uvicorn app.main:app --host "${LARDER_HOST:-0.0.0.0}" --port "${LARDER_PORT:-8000}" "$@"
