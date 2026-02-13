# Claude Code Knowledge Extractor

A [Claude Code](https://docs.anthropic.com/en/docs/claude-code) hook that automatically extracts and persists project knowledge from your conversation transcripts.

Every time a Claude Code session ends, this hook analyzes the transcript and updates two files in your project root:

- **`CLAUDE.md`** -- Instructions and rules (things to DO/AVOID, credentials, workflow conventions)
- **`context.md`** -- Project context (architecture, business logic, entity relationships, key decisions)

These files are loaded by Claude Code on subsequent sessions, giving it persistent memory across conversations.

## How It Works

```
Session ends
    |
    v
Stop hook fires (knowledge-extractor.sh)
    |
    v
Python script parses transcript (JSONL)
    |  - Strips tool calls, system messages, thinking blocks
    |  - Keeps user + assistant text (last 30,000 chars)
    |
    v
Calls `claude -p --model opus` with extraction prompt
    |  - Uses your existing Claude Code subscription
    |  - No separate API key needed
    |
    v
Merges results into project files
    |-- CLAUDE.md: preserves manual content above marker, updates auto-extracted section
    |-- context.md: full rewrite (newest knowledge wins on conflicts)
```

### Safety Features

- **Debouncing** -- Skips extraction if less than 5 minutes since last run AND fewer than 10 new messages
- **Locking** -- Prevents concurrent extraction runs (3-minute stale lock timeout)
- **Atomic writes** -- Uses temp file + rename to prevent file corruption
- **Non-blocking** -- Shell wrapper runs Python script in the background; hook returns immediately
- **Graceful degradation** -- Silently skips on any error (never breaks your session)

## Prerequisites

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed and working
- Python 3.9+
- `claude` CLI accessible on your PATH

## Installation

### Option 1: Automated Install

```bash
git clone https://github.com/srinathduggirala-dotcom/claude-code-knowledge-extractor.git
cd claude-code-knowledge-extractor
./install.sh
```

### Option 2: Manual Install

1. **Copy the hook files** to `~/.claude/hooks/`:

   ```bash
   mkdir -p ~/.claude/hooks
   cp knowledge-extractor.sh ~/.claude/hooks/
   cp knowledge-extractor.py ~/.claude/hooks/
   chmod +x ~/.claude/hooks/knowledge-extractor.sh
   ```

2. **Register the hook** in `~/.claude/settings.json`:

   Open (or create) `~/.claude/settings.json` and add the Stop hook:

   ```json
   {
     "hooks": {
       "Stop": [
         {
           "hooks": [
             {
               "type": "command",
               "command": "bash ~/.claude/hooks/knowledge-extractor.sh",
               "timeout": 10
             }
           ]
         }
       ]
     }
   }
   ```

   If you already have a `settings.json` with other hooks, merge the Stop hook entry into your existing `hooks` object.

3. **Verify** by starting and stopping a Claude Code session:

   ```bash
   claude  # start a session, have a short conversation, then exit
   ```

   Check the log:
   ```bash
   # Find your project's memory dir (cwd with / replaced by -)
   cat ~/.claude/projects/-Users-$USER-your-project/memory/extractor.log
   ```

## Configuration

Edit the constants at the top of `knowledge-extractor.py` to customize behavior:

| Constant | Default | Description |
|----------|---------|-------------|
| `CLAUDE_MODEL` | `"opus"` | Model used for extraction (uses your Claude Code subscription) |
| `MAX_TRANSCRIPT_CHARS` | `30000` | Maximum transcript characters sent for extraction |
| `MIN_MEANINGFUL_CHARS` | `200` | Minimum transcript length to trigger extraction |
| `DEBOUNCE_SECONDS` | `300` | Cooldown period between extractions (seconds) |
| `DEBOUNCE_MIN_MESSAGES` | `10` | Minimum new messages to bypass debounce |
| `LOCK_STALE_SECONDS` | `180` | Lock file timeout (seconds) |

## Output Files

### CLAUDE.md

The hook appends an auto-extracted section below a marker comment:

```markdown
# Your Manual Content
(anything you write above the marker is preserved)

<!-- Auto-extracted knowledge -->
- Always run tests before committing
- Use Python 3.11+ for this project
- Never delete migration files
```

You can freely edit content above the `<!-- Auto-extracted knowledge -->` marker -- the hook will never touch it.

### context.md

This file is fully managed by the hook (full rewrite each extraction):

```markdown
## Architecture
The API gateway is Kong running on AWS EKS...

## Business Rules
GCP is embedded in GTIN, no direct GSTIN mapping...

## Key Decisions
Decided to use PostgreSQL over MongoDB for...
```

## Logs and Debugging

Logs are stored per-project in the Claude memory directory:

```bash
# Launch log (shell wrapper)
cat /tmp/knowledge-extractor-launch.log

# Extraction log (per project)
cat ~/.claude/projects/<safe-cwd>/memory/extractor.log
```

The `<safe-cwd>` is your project's working directory with `/` replaced by `-`. For example, `/Users/me/myproject` becomes `-Users-me-myproject`.

## How the Merge Works

| File | Strategy |
|------|----------|
| **CLAUDE.md** | Preserves everything above `<!-- Auto-extracted knowledge -->`. Replaces the section below the marker. |
| **context.md** | Full rewrite. Newer knowledge takes precedence on conflicts. |
| **MEMORY.md** | Updates an index file in the memory directory. |

## Uninstalling

1. Remove the Stop hook entry from `~/.claude/settings.json`
2. Delete the hook files:
   ```bash
   rm ~/.claude/hooks/knowledge-extractor.sh
   rm ~/.claude/hooks/knowledge-extractor.py
   ```

## License

MIT
