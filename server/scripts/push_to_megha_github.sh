#!/usr/bin/env bash
# Push ESP-P4-UK-Demo to GitHub: mmeghanamadhuri/NINO_HOME_Intelligent_mode_megha
#
# Prerequisites (run once on this machine as mmeghanamadhuri):
#   1. Create a GitHub Personal Access Token (classic) with "repo" scope.
#   2. export GITHUB_TOKEN='ghp_...'
#   3. echo "$GITHUB_TOKEN" | gh auth login --with-token
#
# Or: gh auth login   (browser sign-in as mmeghanamadhuri)
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

GH="${GH:-gh}"
if ! command -v "$GH" >/dev/null 2>&1 && test -x "$HOME/.local/bin/gh"; then
  GH="$HOME/.local/bin/gh"
fi

REPO="mmeghanamadhuri/NINO_HOME_Intelligent_mode_megha"
REMOTE="megha"

log() { echo "[push-megha] $*"; }

if ! "$GH" auth status >/dev/null 2>&1; then
  log "Not signed in to GitHub CLI. Run: gh auth login"
  log "Sign in as GitHub user: mmeghanamadhuri (madhuri@sirenatech.com)"
  exit 1
fi

ACCOUNT="$("$GH" api user -q .login 2>/dev/null || true)"
if [[ "$ACCOUNT" != "mmeghanamadhuri" ]]; then
  log "WARNING: gh is signed in as '$ACCOUNT', not mmeghanamadhuri."
  log "Run: gh auth logout && gh auth login"
  exit 1
fi

if ! "$GH" repo view "$REPO" >/dev/null 2>&1; then
  log "Creating GitHub repo $REPO ..."
  "$GH" repo create "$REPO" --public --description "NiNO home bot + Intelligent Mode (Megha)"
fi

if git remote get-url "$REMOTE" >/dev/null 2>&1; then
  git remote set-url "$REMOTE" "https://github.com/${REPO}.git"
else
  git remote add "$REMOTE" "https://github.com/${REPO}.git"
fi

# SSH on this machine may be a different GitHub user (e.g. Prajwal-mutalik).
# Use gh HTTPS credentials for mmeghanamadhuri pushes.
"$GH" auth setup-git

log "Pushing main to $REPO (HTTPS via gh credentials) ..."
git push -u "$REMOTE" main

log "Done: https://github.com/${REPO}"
