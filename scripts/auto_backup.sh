#!/bin/bash
# Daily GitHub auto-backup for pilot_trader. Logs success AND errors (never
# fails silently). Installed in the host crontab as:
#   30 3 * * * /home/fbazsa/pilot_trader/scripts/auto_backup.sh >> /home/fbazsa/pilot_trader/auto_backup.log 2>&1
# SSH auth: git@github.com -> ~/.ssh/github_hl_copy_bot (via ~/.ssh/config).

set -u
REPO="${HOME}/pilot_trader"
ts() { date -u '+%Y-%m-%d %H:%M:%S UTC'; }

cd "${REPO}" || { echo "$(ts) ERROR: cannot cd to ${REPO}"; exit 1; }
echo "===== $(ts) auto-backup start ====="

git add -A || { echo "$(ts) ERROR: git add failed"; exit 1; }

if git diff --quiet HEAD; then
    echo "$(ts) no working-tree changes to commit"
else
    if git commit -m "Auto-backup $(date -u '+%Y-%m-%d %H:%M UTC')"; then
        echo "$(ts) committed local changes"
    else
        echo "$(ts) ERROR: git commit failed"
        exit 1
    fi
fi

# Push regardless (covers any local commits not yet on the remote).
if git push origin main; then
    echo "$(ts) SUCCESS: pushed to origin/main"
else
    echo "$(ts) ERROR: git push failed -- manual push needed"
    exit 1
fi
echo "===== $(ts) auto-backup done ====="
