#!/usr/bin/env bash
# Nightly PostgreSQL backup for the brief archive. Keeps 30 days.
# Install on the server:  crontab -e  ->  30 3 * * * /opt/breakout-scanner/deploy/backup.sh >> /var/log/intel-backup.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."
dest=/var/backups/intel
mkdir -p "$dest"
file="$dest/intel-$(date +%F).sql.gz"
docker compose -f deploy/docker-compose.yml --env-file deploy/.env exec -T db pg_dump -U intel -d intel | gzip > "$file"
find "$dest" -name 'intel-*.sql.gz' -mtime +30 -delete
echo "$(date -Is) backup ok: $file ($(du -h "$file" | cut -f1))"
