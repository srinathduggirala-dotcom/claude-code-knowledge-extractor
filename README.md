# Claude Code Knowledge Extractor

A [Claude Code](https://docs.anthropic.com/en/docs/claude-code) hook that automatically extracts and persists project knowledge from your conversation transcripts.

Every time a Claude Code session ends, this hook analyzes the transcript and **incrementally** updates knowledge files:

- **`CLAUDE.md`** -- Instructions and rules (things to DO/AVOID, credentials, workflow conventions)
- **`Context-<Folder>.md`** -- Project context placed in the folder where work happened (e.g., `Context-DB.md` in `DB/`)

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
    |  - Detects primary working folder from file paths
    |
    v
Calls `claude -p --model sonnet` with incremental extraction prompt
    |  - Uses your existing Claude Code subscription
    |  - Returns: additions, corrections, removals (NOT full rewrite)
    |  - Checks existing context for staleness
    |
    v
Applies incremental changes
    |-- CLAUDE.md: preserves manual content above marker, updates auto-extracted section
    |-- Context-<Folder>.md: appends new facts, corrects wrong facts, removes stale facts
```

## Key Design Decisions

### Incremental, Not Full Rewrite

The extractor **never rewrites** the entire context file. Instead, it produces a diff:

| Change Type | What Happens |
|-------------|-------------|
| **Additions** | New facts are appended at the end with `##` headers |
| **Corrections** | Wrong facts are found by exact text match and replaced |
| **Removals** | Stale facts are found by exact text match and deleted |

This means your manually curated context is preserved. Only genuinely new, wrong, or stale information is touched.

### Folder-Level Context Files

Context files are placed **where the work happened**, not always at the project root:

```
WorkingDirectory/
├── CLAUDE.md                    (instructions — always at root)
├── DB/
│   └── Context-DB.md            (context for DB work)
├── goa-collections/
│   └── Context-goa-collections.md
└── OrgPlan/
    └── Context-OrgPlan.md
```

The primary folder is detected from file paths in the transcript. The LLM can also override the detection if it has a stronger signal.

### Relevance Check

Each extraction includes a relevance check — the LLM reviews existing context against the current transcript and flags anything outdated or contradicted. Stale facts are removed automatically.

## Safety Features

- **Debouncing** -- Skips extraction if less than 5 minutes since last run AND fewer than 10 new messages
- **Locking** -- Prevents concurrent extraction runs (3-minute stale lock timeout)
- **Atomic writes** -- Uses temp file + rename to prevent file corruption
- **Non-blocking** -- Shell wrapper runs Python script in the background; hook returns immediately
- **Conservative changes** -- Only modifies context when there's clear evidence from the transcript
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

3. **Verify** by starting and stopping a Claude Code session.

## Configuration

Edit the constants at the top of `knowledge-extractor.py`:

| Constant | Default | Description |
|----------|---------|-------------|
| `CLAUDE_MODEL` | `"sonnet"` | Model used for extraction |
| `MAX_TRANSCRIPT_CHARS` | `30000` | Maximum transcript characters sent |
| `MIN_MEANINGFUL_CHARS` | `200` | Minimum transcript length to trigger |
| `DEBOUNCE_SECONDS` | `300` | Cooldown period (seconds) |
| `DEBOUNCE_MIN_MESSAGES` | `10` | Minimum new messages to bypass debounce |
| `LOCK_STALE_SECONDS` | `180` | Lock file timeout (seconds) |

## Output Files

### CLAUDE.md

Unchanged from v1. Appends auto-extracted section below a marker:

```markdown
# Your Manual Content
(anything above the marker is preserved)

<!-- Auto-extracted knowledge -->
- Always run tests before committing
- Use Python 3.11+ for this project
```

### Context-\<Folder\>.md

Incrementally maintained. New facts are appended, wrong facts corrected, stale facts removed:

```markdown
## Architecture
The API gateway is Kong running on AWS EKS...

## Business Rules
GCP is embedded in GTIN, no direct GSTIN mapping...
```

## Logs and Debugging

```bash
# Launch log (shell wrapper)
cat /tmp/knowledge-extractor-launch.log

# Extraction log (per project)
cat ~/.claude/projects/<safe-cwd>/memory/extractor.log
```

## Uninstalling

1. Remove the Stop hook entry from `~/.claude/settings.json`
2. Delete the hook files:
   ```bash
   rm ~/.claude/hooks/knowledge-extractor.sh
   rm ~/.claude/hooks/knowledge-extractor.py
   ```

## License

MIT
