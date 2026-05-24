#!/bin/bash
# Nightly database backup for Collis Family Planner (SQLite).
# Run via Doppler so DB_PATH is injected:
#   doppler run -- bash scripts/backup_databases.sh
#
# Crontab entry (run as ubuntu, 3am daily):
#   0 3 * * * cd /home/ubuntu/collis-family-planner && /usr/bin/doppler run -- bash scripts/backup_databases.sh >> logs/backup.log 2>&1

set -e

BACKUP_DIR="/mnt/app-data/cfp/backups"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
ARCHIVE="$BACKUP_DIR/cfp_$TIMESTAMP.db.gz"
DB="${DB_PATH:-/mnt/app-data/cfp/family.db}"

mkdir -p "$BACKUP_DIR"

echo "[$(date)] Starting backup of $DB ..."

# SQLite online backup — safe while app is running
sqlite3 "$DB" ".backup /tmp/cfp_backup_$TIMESTAMP.db"
gzip -c "/tmp/cfp_backup_$TIMESTAMP.db" > "$ARCHIVE"
rm -f "/tmp/cfp_backup_$TIMESTAMP.db"

# Keep 7 days of backups
find "$BACKUP_DIR" -name "cfp_*.db.gz" -mtime +7 -delete

echo "[$(date)] Backup complete: $ARCHIVE"
