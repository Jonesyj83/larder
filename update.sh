#!/bin/sh
# Pulls the latest code, keeps the database and run.sh handles new dependencies
# and schema changes on its own. Safe to run any time - it backs up the database
# first and only fast-forwards, so it never clobbers a local change by accident.
#
# LARDER_PORT / LARDER_HOST work the same as run.sh. Set LARDER_SERVICE to the
# name of a systemd service if you run it that way instead of run.sh directly
# (e.g. LARDER_SERVICE=larder ./update.sh) and this will restart that instead.
set -e
cd "$(dirname "$0")"

DATA_DIR="${LARDER_DATA:-$(pwd)/data}"
DB="$DATA_DIR/larder.db"
BACKUP_DIR="$DATA_DIR/backups"

if [ -f "$DB" ]; then
    mkdir -p "$BACKUP_DIR"
    STAMP=$(date +%Y%m%d-%H%M%S)
    cp "$DB" "$BACKUP_DIR/larder-$STAMP.db"
    echo "backed up the database to $BACKUP_DIR/larder-$STAMP.db"
    # keep the last 20 backups, no point letting these pile up forever
    ls -1t "$BACKUP_DIR"/larder-*.db 2>/dev/null | tail -n +21 | xargs -r rm --
else
    echo "no database found yet at $DB, nothing to back up"
fi

echo "pulling the latest code..."
git pull --ff-only

if [ -n "$LARDER_SERVICE" ]; then
    echo "restarting the $LARDER_SERVICE service..."
    sudo systemctl restart "$LARDER_SERVICE"
    echo "done. check it came back up with: systemctl status $LARDER_SERVICE"
    exit 0
fi

PORT="${LARDER_PORT:-8000}"
OLD_PID=$(pgrep -f "uvicorn app.main:app.*--port $PORT" || true)
if [ -n "$OLD_PID" ]; then
    echo "stopping the running copy (pid $OLD_PID)..."
    kill "$OLD_PID"
    for i in 1 2 3 4 5 6 7 8 9 10; do
        kill -0 "$OLD_PID" 2>/dev/null || break
        sleep 1
    done
fi

echo "starting it back up..."
nohup ./run.sh >> "$DATA_DIR/larder.log" 2>&1 &
sleep 2
if pgrep -f "uvicorn app.main:app.*--port $PORT" > /dev/null; then
    echo "done, running again on port $PORT. log: $DATA_DIR/larder.log"
else
    echo "didn't come back up - check $DATA_DIR/larder.log"
    exit 1
fi
