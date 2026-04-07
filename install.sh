#!/usr/bin/env bash
set -euo pipefail

# --------------------------------------------------------------------------
# Knowledge Extractor — Installer for Claude Code
# --------------------------------------------------------------------------
# Copies hook files to ~/.claude/hooks/ and registers the Stop hook
# in ~/.claude/settings.json.
# --------------------------------------------------------------------------

HOOKS_DIR="$HOME/.claude/hooks"
SETTINGS_FILE="$HOME/.claude/settings.json"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Installing Knowledge Extractor hook for Claude Code"

# 1. Copy hook files
mkdir -p "$HOOKS_DIR"
cp "$SCRIPT_DIR/knowledge-extractor.sh" "$HOOKS_DIR/knowledge-extractor.sh"
cp "$SCRIPT_DIR/knowledge-extractor.py" "$HOOKS_DIR/knowledge-extractor.py"
chmod +x "$HOOKS_DIR/knowledge-extractor.sh"
chmod +x "$HOOKS_DIR/knowledge-extractor.py"
echo "    Copied hook files to $HOOKS_DIR/"

# 2. Register the hook in settings.json
HOOK_COMMAND="bash $HOOKS_DIR/knowledge-extractor.sh"

if [ ! -f "$SETTINGS_FILE" ]; then
    # No settings file — create one
    cat > "$SETTINGS_FILE" << SETTINGS_EOF
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "$HOOK_COMMAND",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
SETTINGS_EOF
    echo "    Created $SETTINGS_FILE with Stop hook"
else
    # Settings file exists — check if hook is already registered
    if grep -q "knowledge-extractor" "$SETTINGS_FILE" 2>/dev/null; then
        echo "    Hook already registered in $SETTINGS_FILE (skipping)"
    else
        echo ""
        echo "    [ACTION REQUIRED] Your settings file already exists at:"
        echo "    $SETTINGS_FILE"
        echo ""
        echo "    Add this Stop hook entry to your existing settings:"
        echo ""
        echo '    {'
        echo '      "hooks": {'
        echo '        "Stop": ['
        echo '          {'
        echo '            "hooks": ['
        echo '              {'
        echo '                "type": "command",'
        echo "                \"command\": \"$HOOK_COMMAND\","
        echo '                "timeout": 10'
        echo '              }'
        echo '            ]'
        echo '          }'
        echo '        ]'
        echo '      }'
        echo '    }'
        echo ""
        echo "    If you already have a \"Stop\" hook array, add the hook object to it."
        echo "    If you already have a \"hooks\" key, merge the \"Stop\" array into it."
    fi
fi

# 3. Verify prerequisites
echo ""
echo "==> Checking prerequisites"

if command -v claude &>/dev/null; then
    echo "    claude CLI: found at $(command -v claude)"
else
    echo "    [WARNING] claude CLI not found on PATH."
    echo "    Install Claude Code: https://docs.anthropic.com/en/docs/claude-code"
fi

if command -v python3 &>/dev/null; then
    echo "    python3:    found at $(command -v python3)"
else
    echo "    [WARNING] python3 not found on PATH."
fi

echo ""
echo "==> Installation complete!"
echo ""
echo "    The hook runs automatically when a Claude Code session ends."
echo "    It extracts knowledge into .claude/rules/ and Context-<Folder>.md files."
echo ""
echo "    To verify, start a Claude Code session, have a conversation, exit,"
echo "    then check: cat /tmp/knowledge-extractor-launch.log"
echo ""
