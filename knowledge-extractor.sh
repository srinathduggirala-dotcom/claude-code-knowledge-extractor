#!/usr/bin/env bash
set -euo pipefail

# Knowledge Extractor — Shell wrapper for Claude Code Stop hook.
# Reads hook JSON from stdin, saves to temp file, launches Python script
# in the background, and exits immediately (within the 10-second timeout).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

INPUT=$(cat)
TMPFILE=$(mktemp /tmp/ke-input.XXXXXX)
echo "$INPUT" > "$TMPFILE"

# Locate python3
PYTHON3="$(command -v python3 2>/dev/null || echo /usr/bin/python3)"

nohup "$PYTHON3" "$SCRIPT_DIR/knowledge-extractor.py" "$TMPFILE" \
  >>/tmp/knowledge-extractor-launch.log 2>&1 &

exit 0
