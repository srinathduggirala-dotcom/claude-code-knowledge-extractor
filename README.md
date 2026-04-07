# Claude Code Knowledge Extractor

A [Claude Code](https://docs.anthropic.com/en/docs/claude-code) hook that automatically extracts and persists project knowledge from your conversation transcripts.

Every time a Claude Code session ends, this hook analyzes the transcript and **incrementally** updates knowledge files:

- **`.claude/rules/<topic>.md`** -- Scoped instructions with optional path filters (things to DO/AVOID, workflow rules)
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
Reads existing .claude/rules/*.md files
    |  - Parses frontmatter (description, path scope)
    |  - Sends full content to LLM so it can update existing files
    |
    v
Calls `claude -p --model sonnet` with incremental extraction prompt
    |  - Uses your existing Claude Code subscription
    |  - Returns: rules updates + context additions/corrections/removals
    |  - Checks existing context for staleness
    |
    v
Applies changes
    |-- .claude/rules/<topic>.md: creates or updates scoped rules files
    |-- Context-<Folder>.md: appends new facts, corrects wrong facts, removes stale facts
```

## Key Design Decisions

### Rules Files, Not CLAUDE.md

Instructions go to **`.claude/rules/<topic>.md`** files with optional path scoping, not to CLAUDE.md. This keeps CLAUDE.md manual-only and prevents it from growing unboundedly.

Each rules file has YAML frontmatter:

```markdown
---
description: "Google Workspace CLI rules"
paths:
  - "ProgramManagement/**"
---
- When uploading large CSVs to Sheets, batch writes in ~500-row chunks
- When gws returns 401, check credentials.json path
```

Claude Code loads rules files based on their `paths` scope — rules with `paths: ["Trashformers/**"]` only load when working in that folder. Rules without `paths` are global.

### Incremental Updates

The extractor **never rewrites** the entire context file. Instead, it produces a diff:

| Change Type | What Happens |
|-------------|-------------|
| **Additions** | New facts are appended at the end with `##` headers |
| **Corrections** | Wrong facts are found by exact text match and replaced |
| **Removals** | Stale facts are found by exact text match and deleted |

For rules files, the LLM returns the **full updated content** of each changed file (since rules files are small and atomic replacement is simpler than patching).

### Folder-Level Context Files

Context files are placed **where the work happened**, not always at the project root:

```
WorkingDirectory/
├── .claude/
│   └── rules/
│       ├── git-repo.md              (global — git workflow rules)
│       ├── gws.md                   (global — Google Workspace rules)
│       ├── pgm.md                   (scoped to ProgramManagement/**)
│       ├── trashformers.md          (scoped to Trashformers/**)
│       └── slides-design.md         (scoped to PPTattempt/**)
├── DB/
│   └── Context-DB.md                (context for DB work)
├── Trashformers/
│   └── Context-Trashformers.md
└── CLAUDE.md                        (manual content only)
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
- **Existing rules awareness** -- Reads all existing rules files before calling the LLM, so it appends to existing files rather than creating duplicates

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

### .claude/rules/\<topic\>.md

Scoped instruction files with YAML frontmatter. Created or updated automatically:

```markdown
---
description: "Program Management rules"
paths:
  - "ProgramManagement/**"
---
- When running /pgm, Phase 0 must always run first
- PGM uses a sprint debt model, not carry-over
```

The extractor reads all existing rules files before each run, so it will:
- **Append** new instructions to an existing file if the topic matches
- **Create** a new file only if the instruction covers a genuinely new topic
- **Remove duplicates** and resolve contradictions (new instruction wins)

### Context-\<Folder\>.md

Incrementally maintained. New facts are appended, wrong facts corrected, stale facts removed:

```markdown
## Architecture
The API gateway is Kong running on AWS EKS...

## Business Rules
GCP is embedded in GTIN, no direct GSTIN mapping...
```

## Migrating from v1 (CLAUDE.md auto-section)

If you were using the previous version that wrote to CLAUDE.md:

1. **Split your auto-extracted instructions** into `.claude/rules/` files by topic
2. **Remove the auto-extracted section** from CLAUDE.md (everything below `<!-- Auto-extracted knowledge -->`)
3. **Update the hook** by pulling the latest version or running `./install.sh` again

The new version will not touch CLAUDE.md — it only reads/writes `.claude/rules/` files.

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
