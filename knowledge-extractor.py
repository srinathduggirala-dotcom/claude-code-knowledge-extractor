#!/usr/bin/env python3
"""
Knowledge Extractor — Stop hook for Claude Code.

Parses the session transcript, pipes it to `claude -p --model <model>` to extract
knowledge, then writes to two files in the project root:
  - CLAUDE.md  (instructions: things to DO/AVOID, credentials, workflow rules)
  - context.md (project context: architecture, business logic, facts)

Uses the user's existing Claude Code subscription via `claude -p`.
No separate API key required.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Config — edit these to customize behavior
# ---------------------------------------------------------------------------
CLAUDE_MODEL = "opus"             # Model used for extraction
MAX_TRANSCRIPT_CHARS = 30_000     # Max transcript chars sent for extraction
MIN_MEANINGFUL_CHARS = 200        # Min transcript length to trigger extraction
DEBOUNCE_SECONDS = 300            # Cooldown between extractions (5 minutes)
DEBOUNCE_MIN_MESSAGES = 10        # Min new messages to bypass debounce
LOCK_STALE_SECONDS = 180          # Lock timeout (3 minutes)
AUTO_MARKER = "<!-- Auto-extracted knowledge -->"

# ---------------------------------------------------------------------------
# Locate claude CLI
# ---------------------------------------------------------------------------

def find_claude_cli() -> str:
    """Find the claude CLI binary."""
    found = shutil.which("claude")
    if found:
        return found
    # Common locations
    for candidate in [
        "/usr/local/bin/claude",
        "/opt/homebrew/bin/claude",
        os.path.expanduser("~/.npm-global/bin/claude"),
        os.path.expanduser("~/.claude/local/claude"),
    ]:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return "claude"  # fallback — hope it's on PATH

CLAUDE_CLI = find_claude_cli()

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def memory_dir(cwd: str) -> Path:
    """Derive the Claude project memory directory from cwd."""
    safe = cwd.replace("/", "-")
    return Path.home() / ".claude" / "projects" / safe / "memory"


def state_path(mem: Path) -> Path:
    return mem / ".extractor-state.json"


def lock_path(mem: Path) -> Path:
    return mem / ".extractor.lock"


def log_path(mem: Path) -> Path:
    return mem / "extractor.log"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(mem: Path, msg: str):
    try:
        lp = log_path(mem)
        lp.parent.mkdir(parents=True, exist_ok=True)
        with open(lp, "a") as f:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------

def acquire_lock(mem: Path) -> bool:
    lp = lock_path(mem)
    try:
        if lp.exists():
            age = time.time() - lp.stat().st_mtime
            if age < LOCK_STALE_SECONDS:
                return False
            lp.unlink(missing_ok=True)
        lp.parent.mkdir(parents=True, exist_ok=True)
        lp.write_text(str(os.getpid()))
        return True
    except Exception:
        return False


def release_lock(mem: Path):
    try:
        lock_path(mem).unlink(missing_ok=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Debouncing
# ---------------------------------------------------------------------------

def should_skip(mem: Path, message_count: int) -> bool:
    sp = state_path(mem)
    if not sp.exists():
        return False
    try:
        state = json.loads(sp.read_text())
        last_run = state.get("last_run_timestamp", 0)
        last_count = state.get("last_line_count", 0)
        elapsed = time.time() - last_run
        new_messages = message_count - last_count
        if elapsed < DEBOUNCE_SECONDS and new_messages < DEBOUNCE_MIN_MESSAGES:
            return True
    except Exception:
        pass
    return False


def save_state(mem: Path, session_id: str, line_count: int):
    sp = state_path(mem)
    sp.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "last_run_timestamp": time.time(),
        "last_line_count": line_count,
        "session_id": session_id,
    }
    sp.write_text(json.dumps(state))


# ---------------------------------------------------------------------------
# Transcript parsing
# ---------------------------------------------------------------------------

def parse_transcript(transcript_path: str) -> tuple:
    """
    Parse the JSONL transcript. Keep only user text + assistant text messages.
    Skip: tool_use, tool_result, system, progress, queue-operation, thinking,
          file-history-snapshot, isMeta messages.

    Returns (text, message_count).
    """
    path = Path(transcript_path)
    if not path.exists():
        return "", 0

    messages = []
    count = 0

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_type = obj.get("type", "")

        # Skip non-message types
        if msg_type in ("file-history-snapshot", "queue-operation", "progress"):
            continue

        # Skip meta messages
        if obj.get("isMeta"):
            continue

        # Process user and assistant messages
        if msg_type in ("user", "assistant"):
            message = obj.get("message", {})
            content = message.get("content", "")

            # content can be a string or a list of content blocks
            text_parts = []
            if isinstance(content, str):
                # Skip command messages and empty content
                if "<command-name>" in content or "<local-command" in content:
                    continue
                text_parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        # Skip thinking, tool_use, tool_result blocks
                    elif isinstance(block, str):
                        text_parts.append(block)

            text = "\n".join(text_parts).strip()
            if not text:
                continue

            role = message.get("role", msg_type)
            messages.append(f"[{role.upper()}]: {text}")
            count += 1

    # Truncate to last MAX_TRANSCRIPT_CHARS
    combined = "\n\n".join(messages)
    if len(combined) > MAX_TRANSCRIPT_CHARS:
        combined = combined[-MAX_TRANSCRIPT_CHARS:]

    return combined, count


# ---------------------------------------------------------------------------
# File I/O helpers
# ---------------------------------------------------------------------------

def read_file(path: Path) -> str:
    try:
        if path.exists():
            return path.read_text(encoding="utf-8")
    except Exception:
        pass
    return ""


def atomic_write(path: Path, content: str):
    """Write atomically using temp file + rename on same filesystem."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    closed = False
    try:
        os.write(fd, content.encode("utf-8"))
        os.close(fd)
        closed = True
        os.rename(tmp, str(path))
    except Exception:
        if not closed:
            try:
                os.close(fd)
            except Exception:
                pass
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


# ---------------------------------------------------------------------------
# CLAUDE.md merge logic
# ---------------------------------------------------------------------------

def merge_claude_md(existing: str, new_auto_section: str) -> str:
    """
    Preserve everything above the AUTO_MARKER in existing CLAUDE.md.
    Replace the auto-extracted section below the marker.
    """
    if AUTO_MARKER in existing:
        manual_part = existing.split(AUTO_MARKER)[0].rstrip()
    else:
        manual_part = existing.rstrip()

    if not new_auto_section.strip():
        # Nothing new — keep as-is
        return existing

    return f"{manual_part}\n\n{AUTO_MARKER}\n{new_auto_section.strip()}\n"


# ---------------------------------------------------------------------------
# Call claude -p
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = r"""You are a knowledge extraction specialist analyzing a Claude Code conversation transcript.
Your job: extract knowledge and merge it into two outputs.

OUTPUT 1 — claude_md (Instructions):
Things the AI assistant should DO or AVOID. Includes:
- Workflow rules ("always run tests before committing")
- Credentials explicitly shared by the user for use
- Coding standards ("use Python 3.11+", "prefer bun over npm")
- Behavioral directives ("never invent numbers", "ask before deleting")
- Tool/API usage patterns ("always use status=published for DataKart")
Format: imperative voice. "- Always X", "- Never Y", "- Use Z for..."
IMPORTANT: Merge with previously extracted instructions (provided below).
Remove exact duplicates. If a new instruction contradicts an old one, keep the new one.

OUTPUT 2 — context_md (Project Context):
Facts about the project, architecture, business logic. Includes:
- Technical architecture ("API gateway is Kong on AWS EKS")
- Business rules ("GCP is embedded in GTIN, no direct GSTIN mapping")
- Entity relationships ("brand Parle -> GCP 8901719 -> 495 SKUs")
- API behaviors and findings from exploration
- Key decisions made during conversations
Format: declarative. Organized with ## headers for searchability. Can be up to 1000 lines.
IMPORTANT: Merge with existing context (provided below).
Remove duplicates, resolve contradictions (newer wins), reorganize as needed.

RULES:
- Only extract CONFIRMED knowledge (stated by user or verified by actions)
- Only extract REUSABLE knowledge (skip one-off debugging details)
- If nothing new to extract, return existing content unchanged
- Return COMPLETE content for both fields, not just additions
- Be concise but thorough"""


def call_claude(transcript: str, existing_auto: str, existing_context: str, mem: Path) -> dict:
    """Call claude -p with the extraction prompt, return parsed JSON."""

    user_prompt = f"""{SYSTEM_PROMPT}

---

Here is the conversation transcript from a Claude Code session:

<transcript>
{transcript}
</transcript>

Here are the previously extracted instructions (auto-extracted section of CLAUDE.md):
<existing_instructions>
{existing_auto if existing_auto else "(none yet)"}
</existing_instructions>

Here is the existing context.md:
<existing_context>
{existing_context if existing_context else "(none yet)"}
</existing_context>

Analyze the transcript and produce the updated instructions and context.
If nothing new was discussed, return the existing content unchanged.

You MUST respond with ONLY a JSON object (no markdown fences, no extra text) with exactly these keys:
- "claude_md": string containing the auto-extracted instructions (markdown bullet list)
- "context_md": string containing the full context file (markdown with headers)
- "summary": string with 1-2 sentence summary of changes

Example format:
{{"claude_md": "- Always use X\\n- Never do Y", "context_md": "## Section\\nFact here", "summary": "Extracted 2 instructions and 1 fact"}}"""

    try:
        result = subprocess.run(
            [
                CLAUDE_CLI,
                "-p",
                "--model", CLAUDE_MODEL,
                "--output-format", "json",
            ],
            input=user_prompt,
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode != 0:
            log(mem, f"claude -p failed: exit={result.returncode}, stderr={result.stderr[:300]}")
            return None

        if not result.stdout.strip():
            log(mem, "claude -p returned empty stdout")
            return None

        output = json.loads(result.stdout)

        if not isinstance(output, dict) or "result" not in output:
            log(mem, f"claude -p unexpected output: {str(output)[:200]}")
            return None

        content = output["result"]

        # content is a string — may be raw JSON or wrapped in markdown fences
        if isinstance(content, str):
            text = content.strip()
            # Strip markdown fences if present
            if text.startswith("```"):
                lines = text.split("\n")
                lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                text = "\n".join(lines).strip()
            parsed = json.loads(text)
        elif isinstance(content, dict):
            parsed = content
        else:
            log(mem, f"claude -p result type unexpected: {type(content)}")
            return None

        # Normalize: claude_md and context_md should be strings.
        # The model sometimes returns arrays — convert them.
        for key in ("claude_md", "context_md"):
            val = parsed.get(key, "")
            if isinstance(val, list):
                parsed[key] = "\n".join(f"- {item}" if not str(item).startswith("- ") else str(item) for item in val)
            elif not isinstance(val, str):
                parsed[key] = str(val) if val else ""

        return parsed

    except Exception as e:
        log(mem, f"call_claude exception: {e}")
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Read hook input from temp file (passed as $1 by shell wrapper)
    if len(sys.argv) < 2:
        sys.exit(0)

    tmpfile = sys.argv[1]
    try:
        with open(tmpfile) as f:
            hook_input = json.load(f)
    except Exception:
        sys.exit(0)
    finally:
        try:
            os.unlink(tmpfile)
        except Exception:
            pass

    # Check for stop_hook_active to prevent infinite loops
    if hook_input.get("stop_hook_active"):
        sys.exit(0)

    transcript_path = hook_input.get("transcript_path", "")
    cwd = hook_input.get("cwd", "")
    session_id = hook_input.get("session_id", "")

    if not transcript_path or not cwd:
        sys.exit(0)

    mem = memory_dir(cwd)
    mem.mkdir(parents=True, exist_ok=True)

    # Parse transcript
    transcript, message_count = parse_transcript(transcript_path)
    if len(transcript) < MIN_MEANINGFUL_CHARS:
        log(mem, f"Skipping: transcript too short ({len(transcript)} chars)")
        sys.exit(0)

    # Debounce: skip if <5 min since last run AND <10 new messages
    if should_skip(mem, message_count):
        log(mem, f"Skipping: debounce (count={message_count})")
        sys.exit(0)

    # Lock
    if not acquire_lock(mem):
        log(mem, "Skipping: another extraction running")
        sys.exit(0)

    try:
        log(mem, f"Starting extraction: session={session_id}, messages={message_count}")

        project_dir = Path(cwd)
        claude_md_path = project_dir / "CLAUDE.md"
        context_md_path = project_dir / "context.md"

        existing_claude = read_file(claude_md_path)
        existing_context = read_file(context_md_path)

        # Extract just the auto section from existing CLAUDE.md
        existing_auto = ""
        if AUTO_MARKER in existing_claude:
            existing_auto = existing_claude.split(AUTO_MARKER, 1)[1].strip()

        # Call claude
        result = call_claude(transcript, existing_auto, existing_context, mem)

        if result is None:
            log(mem, "Extraction failed: claude -p returned no result")
            save_state(mem, session_id, message_count)
            return

        new_auto = result.get("claude_md", "").strip()
        new_context = result.get("context_md", "").strip()
        summary = result.get("summary", "")

        # Write CLAUDE.md (merge strategy — preserve manual content)
        if new_auto:
            merged = merge_claude_md(existing_claude, new_auto)
            atomic_write(claude_md_path, merged)
            log(mem, "Updated CLAUDE.md auto-section")

        # Write context.md (full rewrite)
        if new_context:
            atomic_write(context_md_path, new_context + "\n")
            log(mem, "Updated context.md")

        # Update MEMORY.md index
        memory_md = mem / "MEMORY.md"
        index_content = """# Project Memory Index

Knowledge is automatically extracted from conversations and stored in:
- **CLAUDE.md** (project root): Instructions, workflow rules, things to DO/AVOID
- **context.md** (project root): Project facts, architecture, business logic (searchable)

These files are maintained by the knowledge extractor hook.
"""
        atomic_write(memory_md, index_content)

        save_state(mem, session_id, message_count)
        log(mem, f"Done: {summary}")

    except Exception as e:
        log(mem, f"Error: {e}")
    finally:
        release_lock(mem)


if __name__ == "__main__":
    main()
