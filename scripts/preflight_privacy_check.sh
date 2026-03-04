#!/usr/bin/env bash
# Preflight privacy check - run before first push to verify no personal data leaks.
# Scans all files that would be committed: already-staged AND untracked-but-committable.
# Usage: bash scripts/preflight_privacy_check.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FAIL=0

echo "=== Preflight Privacy Check ==="
echo ""

# Build list of files git would commit: staged + untracked (respecting .gitignore)
# --cached: already staged; --others --exclude-standard: untracked but not ignored
COMMITTABLE=$(git -C "$REPO_ROOT" ls-files --cached --others --exclude-standard 2>/dev/null)

# 1. Check for hardcoded Windows user paths (case-insensitive)
echo "[1/4] Scanning for hardcoded user paths..."
MATCHES=$(echo "$COMMITTABLE" | (grep -E '\.(py|json|sh|txt|md)$' || true) | while read -r f; do
    grep -ni 'c:\\users\\' "$REPO_ROOT/$f" 2>/dev/null | grep -iv 'YourName' | sed "s|^|$f:|" || true
done)
if [ -n "$MATCHES" ]; then
    echo "$MATCHES"
    echo "  FAIL: Found hardcoded user paths in committable files"
    FAIL=1
else
    echo "  PASS"
fi

# 2. Check that cache/metadata files are not committable
echo "[2/4] Checking for cache/metadata files..."
CACHE_MATCHES=$(echo "$COMMITTABLE" | grep -E '(music_shuffler_cache/|\.pkl$|library_cache|_metadata\.xml)' || true)
if [ -n "$CACHE_MATCHES" ]; then
    echo "$CACHE_MATCHES"
    echo "  FAIL: Cache or metadata files would be committed"
    FAIL=1
else
    echo "  PASS"
fi

# 3. Check for binary/executable files
echo "[3/4] Checking for tracked binaries..."
BIN_MATCHES=$(echo "$COMMITTABLE" | grep -E '\.(exe|dll)$' || true)
if [ -n "$BIN_MATCHES" ]; then
    echo "$BIN_MATCHES"
    echo "  FAIL: Binary files would be committed"
    FAIL=1
else
    echo "  PASS"
fi

# 4. Check for common secret patterns
echo "[4/4] Scanning for potential secrets..."
SECRET_MATCHES=$(echo "$COMMITTABLE" | (grep -E '\.(py|json|sh|env)$' || true) | while read -r f; do
    grep -nE '(api[_-]?key|secret[_-]?key|password|token)\s*[:=]\s*["'"'"'][^"'"'"']{8,}' "$REPO_ROOT/$f" 2>/dev/null \
        | grep -iv 'example' | sed "s|^|$f:|" || true
done)
if [ -n "$SECRET_MATCHES" ]; then
    echo "$SECRET_MATCHES"
    echo "  FAIL: Potential secrets found"
    FAIL=1
else
    echo "  PASS"
fi

echo ""
if [ "$FAIL" -eq 0 ]; then
    echo "=== ALL CHECKS PASSED ==="
    exit 0
else
    echo "=== SOME CHECKS FAILED - Review above before pushing ==="
    exit 1
fi
