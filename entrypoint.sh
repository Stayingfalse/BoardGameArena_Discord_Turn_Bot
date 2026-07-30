#!/bin/sh
set -e

# Derive the database directory from BGA_DB_PATH (default: /data/bga_bot.db)
db_dir="$(dirname "${BGA_DB_PATH:-/data/bga_bot.db}")"

# Ensure the directory exists so SQLite can create the database file
mkdir -p "$db_dir"

# Verify the directory is writable before handing off to the application;
# this surfaces a clear error instead of an opaque sqlite3.OperationalError.
if [ ! -w "$db_dir" ]; then
    echo "ERROR: database directory '$db_dir' is not writable by $(id -un). " \
         "Fix the directory permissions or mount a writable volume at that path." >&2
    exit 1
fi

exec "$@"
