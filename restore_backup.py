"""
Restores main.db from one of backupDatabaseTask's daily snapshots (see
_backupDatabase in bot.py). Run this with the bot stopped - it works
directly on the database file on disk, the same one mainDB holds a live
connection to while the bot is running, and copying over a file a running
process still has open risks corrupting it.

Usage:
    python restore_backup.py            # lists backups, prompts for a pick
    python restore_backup.py 1          # restores the Nth backup (from the list)
    python restore_backup.py main-20260819-030000.db   # restores by filename

Before overwriting main.db, the current live database is itself copied into
the backups folder as "main-before-restore-<timestamp>.db" - so restoring
the wrong backup, or restoring at all, is itself undoable the same way.
"""

import os
import shutil
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "guildData", "serverInfo", "main.db")
BACKUP_DIR = os.path.join(BASE_DIR, "data", "guildData", "backups")


def _formatSize(num_bytes):
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def _listBackups():
    if not os.path.isdir(BACKUP_DIR):
        return []
    entries = [
        name for name in os.listdir(BACKUP_DIR)
        if name.startswith("main-") and name.endswith(".db") and not name.startswith("main-before-restore-")
    ]
    entries.sort(key=lambda name: os.path.getmtime(os.path.join(BACKUP_DIR, name)), reverse=True)
    return entries


def _resolveChoice(backups, choice):
    if choice in backups:
        return choice
    if choice.isdigit():
        index = int(choice) - 1
        if 0 <= index < len(backups):
            return backups[index]
    return None


def main():
    backups = _listBackups()
    if not backups:
        print(f"No backups found in {BACKUP_DIR}")
        return

    print("Available backups (newest first):")
    for i, name in enumerate(backups, start=1):
        path = os.path.join(BACKUP_DIR, name)
        mtime = datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M:%S")
        print(f"  {i}. {name}  ({mtime}, {_formatSize(os.path.getsize(path))})")

    if len(sys.argv) > 1:
        choice = sys.argv[1]
    else:
        choice = input("\nRestore which one? (number or filename, blank to cancel): ").strip()
        if not choice:
            print("Cancelled.")
            return

    chosen = _resolveChoice(backups, choice)
    if chosen is None:
        print(f"'{choice}' doesn't match any backup listed above.")
        sys.exit(1)

    print(
        "\nThis will REPLACE the live database with this backup. Make sure the bot "
        "process is stopped before continuing."
    )
    confirm = input(f"Restore {chosen}? Type 'yes' to continue: ").strip().lower()
    if confirm != "yes":
        print("Cancelled.")
        return

    chosen_path = os.path.join(BACKUP_DIR, chosen)

    if os.path.isfile(DB_PATH):
        os.makedirs(BACKUP_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safety_path = os.path.join(BACKUP_DIR, f"main-before-restore-{timestamp}.db")
        shutil.copy2(DB_PATH, safety_path)
        print(f"Current database saved to {safety_path} before restoring.")

    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    shutil.copy2(chosen_path, DB_PATH)
    print(f"Restored {chosen} to {DB_PATH}. You can start the bot again now.")


if __name__ == "__main__":
    main()
