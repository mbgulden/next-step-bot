#!/usr/bin/env bash
set -euo pipefail
# ─────────────────────────────────────────────────────────────────
# Next Step Bot — Nightly Backup to Synology NAS
# Run via cron:  0 3 * * * /home/ubuntu/work/next-step-bot/backup.sh
# ─────────────────────────────────────────────────────────────────

BACKUP_ROOT="/home/ubuntu/mounts/synology-agentic-context/next-step-backups"
TIMESTAMP=$(date +%Y%m%d_%H%M)
BACKUP_DIR="${BACKUP_ROOT}/${TIMESTAMP}"
RETENTION_DAYS=30

mkdir -p "${BACKUP_DIR}"

# ── Michael's bot ──
echo "[backup] Michael..."
cp -r /home/ubuntu/work/next-step-bot/data "${BACKUP_DIR}/michael-data" 2>/dev/null || true
cp /home/ubuntu/work/next-step-bot/family.json "${BACKUP_DIR}/michael-family.json" 2>/dev/null || true
cp /home/ubuntu/work/next-step-bot/SOUL.md "${BACKUP_DIR}/michael-soul.md" 2>/dev/null || true
# Journals
if [ -d /home/ubuntu/work/next-step-bot/journals ]; then
    cp -r /home/ubuntu/work/next-step-bot/journals "${BACKUP_DIR}/michael-journals"
fi

# ── Becca's bot ──
echo "[backup] Becca..."
cp -r /home/ubuntu/work/next-step-becca/data "${BACKUP_DIR}/becca-data" 2>/dev/null || true
cp /home/ubuntu/work/next-step-becca/SOUL.md "${BACKUP_DIR}/becca-soul.md" 2>/dev/null || true
if [ -d /home/ubuntu/work/next-step-becca/journals ]; then
    cp -r /home/ubuntu/work/next-step-becca/journals "${BACKUP_DIR}/becca-journals"
fi

# ── MCP Server (config only, not ephemeris binaries) ──
echo "[backup] MCP..."
cp /home/ubuntu/work/next-step-bot/.env "${BACKUP_DIR}/michael-env" 2>/dev/null || true
cp /home/ubuntu/work/next-step-becca/.env "${BACKUP_DIR}/becca-env" 2>/dev/null || true
cp /home/ubuntu/work/OpenHumanDesignMCP/hd-mcp-server/src/mcp_server.py "${BACKUP_DIR}/mcp_server.py" 2>/dev/null || true

# ── Prune old backups ──
echo "[backup] Pruning backups older than ${RETENTION_DAYS} days..."
find "${BACKUP_ROOT}" -maxdepth 1 -type d -mtime +${RETENTION_DAYS} -exec rm -rf {} \; 2>/dev/null || true

echo "[backup] Done — ${BACKUP_DIR}"
