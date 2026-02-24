#!/usr/bin/env python3
"""
Knowledge Extractor — Stop hook for Claude Code.

Parses the session transcript, pipes it to `claude -p --model <model>` to extract
knowledge INCREMENTALLY, then applies targeted edits to:
  - CLAUDE.md  (instructions: things to DO/AVOID, credentials, workflow rules)
  - Context-<Folder>.md (project context placed in the primary working folder)

Key behaviors:
  - INCREMENTAL: Only adds new facts, corrects wrong facts, removes stale facts.
    Never rewrites the entire context file.
  - FOLDER-LEVEL: Detects the primary working folder from file paths in the
    transcript and places the context file there (e.g., Context-DB.md in DB/).
  - RELEVANCE CHECK: Each session, flags stale/outdated context for removal.

Uses the user's existing Claude Code subscription via `claude -p`.
No separate API key required.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Config — edit these to customize behavior
# ---------------------------------------------------------------------------
CLAUDE_MODEL = "sonnet"           # Model used for extraction (sonnet for speed)
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
    for candidate in [
        "/usr/local/bin/claude",
        "/opt/homebrew/bin/claude",
        os.path.expanduser("~/.npm-global/bin/claude"),
        os.path.expanduser("~/.claude/local/claude"),
    ]:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return "claude"

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

        if msg_type in ("file-history-snapshot", "queue-operation", "progress"):
            continue
        if obj.get("isMeta"):
            continue

        if msg_type in ("user", "assistant"):
            message = obj.get("message", {})
            content = message.get("content", "")

            text_parts = []
            if isinstance(content, str):
                if "<command-name>" in content or "<local-command" in content:
                    continue
                text_parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                    elif isinstance(block, str):
                        text_parts.append(block)

            text = "\n".join(text_parts).strip()
            if not text:
                continue

            role = message.get("role", msg_type)
            messages.append(f"[{role.upper()}]: {text}")
            count += 1

    combined = "\n\n".join(messages)
    if len(combined) > MAX_TRANSCRIPT_CHARS:
        combined = combined[-MAX_TRANSCRIPT_CHARS:]

    return combined, count


# ---------------------------------------------------------------------------
# Detect primary working folder from transcript
# ---------------------------------------------------------------------------

def detect_primary_folder(transcript: str, project_root: str) -> str:
    """
    Scan the transcript for file paths to determine which subfolder
    had the most activity. Returns the subfolder name (e.g., "DB")
    or "" if activity was at the root.
    """
    # Match file paths that start with the project root
    root = project_root.rstrip("/")
    # Find all absolute paths in the transcript
    path_pattern = re.compile(re.escape(root) + r'/([^/\s"\'<>]+)')
    matches = path_pattern.findall(transcript)

    if not matches:
        return ""

    # Count subfolder mentions (exclude files at root — those have a dot)
    folder_counts = {}
    for match in matches:
        # If it looks like a directory (no extension, or known dir names)
        # Just count the first path component
        folder = match.split("/")[0] if "/" in match else match
        # Skip if it's clearly a file at root level (has extension)
        if "." in folder and not folder.startswith("."):
            continue
        # Skip hidden directories
        if folder.startswith("."):
            continue
        folder_counts[folder] = folder_counts.get(folder, 0) + 1

    if not folder_counts:
        return ""

    # Return the most mentioned folder
    primary = max(folder_counts, key=folder_counts.get)

    # Verify it actually exists as a directory
    candidate = Path(root) / primary
    if candidate.is_dir():
        return primary

    return ""


def find_context_file(project_dir: Path, folder_name: str) -> Path:
    """
    Return the path for the context file.
    If folder_name is set: <project_dir>/<folder_name>/Context-<folder_name>.md
    Otherwise: <project_dir>/context.md (legacy fallback)
    """
    if folder_name:
        return project_dir / folder_name / f"Context-{folder_name}.md"
    return project_dir / "context.md"


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
# CLAUDE.md merge logic (unchanged — already incremental)
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
        return existing

    return f"{manual_part}\n\n{AUTO_MARKER}\n{new_auto_section.strip()}\n"


# ---------------------------------------------------------------------------
# Incremental context merge
# ---------------------------------------------------------------------------

def apply_incremental_changes(existing: str, additions: str, corrections: list, removals: list) -> str:
    """
    Apply incremental changes to the existing context file.
    - additions: new markdown content to append
    - corrections: list of {find: str, replace: str} dicts
    - removals: list of strings (lines or phrases) to remove
    """
    result = existing

    # 1. Apply corrections (search/replace)
    for correction in corrections:
        find_text = correction.get("find", "").strip()
        replace_text = correction.get("replace", "").strip()
        if find_text and find_text in result:
            result = result.replace(find_text, replace_text, 1)

    # 2. Apply removals
    for removal in removals:
        removal = removal.strip()
        if not removal:
            continue
        # Try to remove the line containing this text
        lines = result.split("\n")
        new_lines = []
        for line in lines:
            if removal in line:
                continue  # skip this line
            new_lines.append(line)
        result = "\n".join(new_lines)

    # 3. Append additions at the end
    if additions and additions.strip():
        result = result.rstrip()
        if result:
            result += "\n\n" + additions.strip() + "\n"
        else:
            result = additions.strip() + "\n"

    # Clean up multiple blank lines
    result = re.sub(r'\n{3,}', '\n\n', result)

    return result


# ---------------------------------------------------------------------------
# Call claude -p
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = r"""You are a knowledge extraction specialist analyzing a Claude Code conversation transcript.
Your job: extract NEW knowledge and produce INCREMENTAL updates to the existing context file.

CRITICAL RULES — READ CAREFULLY:
1. You are NOT rewriting the context file. You are producing a DIFF.
2. The existing context is the source of truth. Only change what needs changing.
3. "additions" = genuinely NEW facts not already in the existing context.
4. "corrections" = facts in the existing context that are PROVABLY WRONG based on this transcript.
5. "removals" = facts in the existing context that are STALE or CONTRADICTED by this transcript.
6. If the transcript doesn't reveal anything new, return empty additions/corrections/removals.
7. Be conservative. When in doubt, don't change existing content.

OUTPUT 1 — claude_md (Instructions):
Things the AI assistant should DO or AVOID. Includes:
- Workflow rules, credentials, coding standards, behavioral directives
Format: imperative voice. "- Always X", "- Never Y"
IMPORTANT: Merge with previously extracted instructions (provided below).
Remove exact duplicates. If a new instruction contradicts an old one, keep the new one.

OUTPUT 2 — context changes (Incremental):
- "primary_folder": Which subfolder had the most activity in this session?
  Determine from file paths mentioned in the transcript. Return just the folder
  name (e.g., "DB", "goa-collections", "OrgPlan"). Return "" if activity was
  at the project root or spread evenly.
- "additions": New markdown content to APPEND to the context file.
  Must be formatted with ## headers. Only genuinely new facts.
- "corrections": List of {find, replace} pairs where existing text is wrong.
  "find" must be an EXACT substring from the existing context.
  "replace" is what it should be changed to.
- "removals": List of exact lines/phrases from existing context that are stale
  or no longer accurate. Only remove if the transcript PROVES it wrong.
- "relevance_notes": Brief note on what you checked for staleness.

RELEVANCE CHECK:
Review the existing context against what you learned in this transcript.
Flag anything that appears outdated or contradicted. Be specific in relevance_notes.

RULES:
- Only extract CONFIRMED knowledge (stated by user or verified by actions)
- Only extract REUSABLE knowledge (skip one-off debugging details)
- Be CONSERVATIVE with corrections and removals — only when clearly wrong
- "additions" should NOT duplicate what's already in the existing context
- Be concise but thorough"""


def call_claude(transcript: str, existing_auto: str, existing_context: str,
                context_file_name: str, mem: Path) -> dict:
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

Here is the existing context file ({context_file_name}):
<existing_context>
{existing_context if existing_context else "(none yet — this is a new context file)"}
</existing_context>

Analyze the transcript. Produce incremental updates.
If nothing new was discussed, return empty additions/corrections/removals.

You MUST respond with ONLY a JSON object (no markdown fences, no extra text) with exactly these keys:
- "claude_md": string — updated auto-extracted instructions (full replacement, as before)
- "primary_folder": string — the subfolder name where most work happened (e.g., "DB"), or "" for root
- "additions": string — new markdown content to append (with ## headers), or "" if nothing new
- "corrections": array of objects {{"find": "exact old text", "replace": "new text"}}, or []
- "removals": array of strings (exact lines to remove from existing context), or []
- "relevance_notes": string — what you checked for staleness
- "summary": string — 1-2 sentence summary of changes

Example:
{{"claude_md": "- Always use X\\n- Never do Y", "primary_folder": "DB", "additions": "## New Section\\n- New fact discovered", "corrections": [{{"find": "old wrong fact", "replace": "corrected fact"}}], "removals": ["completely outdated line"], "relevance_notes": "Checked DB connection details — still accurate", "summary": "Added 1 fact about X, corrected Y"}}"""

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

        if isinstance(content, str):
            text = content.strip()
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

        # Normalize claude_md to string
        val = parsed.get("claude_md", "")
        if isinstance(val, list):
            parsed["claude_md"] = "\n".join(
                f"- {item}" if not str(item).startswith("- ") else str(item)
                for item in val
            )
        elif not isinstance(val, str):
            parsed["claude_md"] = str(val) if val else ""

        # Normalize additions to string
        val = parsed.get("additions", "")
        if not isinstance(val, str):
            parsed["additions"] = str(val) if val else ""

        # Normalize corrections to list of dicts
        val = parsed.get("corrections", [])
        if not isinstance(val, list):
            parsed["corrections"] = []

        # Normalize removals to list of strings
        val = parsed.get("removals", [])
        if not isinstance(val, list):
            parsed["removals"] = []
        else:
            parsed["removals"] = [str(r) for r in val if r]

        # Normalize primary_folder
        val = parsed.get("primary_folder", "")
        if not isinstance(val, str):
            parsed["primary_folder"] = ""

        return parsed

    except Exception as e:
        log(mem, f"call_claude exception: {e}")
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
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

    # Debounce
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

        # Detect primary folder from transcript file paths
        detected_folder = detect_primary_folder(transcript, cwd)
        log(mem, f"Detected primary folder from transcript: '{detected_folder or '(root)'}'")

        # Determine context file path
        context_path = find_context_file(project_dir, detected_folder)
        context_file_name = context_path.name
        existing_context = read_file(context_path)

        # If no existing context at detected folder, check for legacy context.md
        # at root and mention it so the LLM knows what exists
        legacy_context_path = project_dir / "context.md"
        legacy_context = ""
        if not existing_context and legacy_context_path.exists() and detected_folder:
            legacy_context = read_file(legacy_context_path)
            if legacy_context:
                log(mem, f"Found legacy context.md at root, will consider it")

        # Use the richer context for LLM input
        context_for_llm = existing_context or legacy_context

        # CLAUDE.md — always at project root
        claude_md_path = project_dir / "CLAUDE.md"
        existing_claude = read_file(claude_md_path)
        existing_auto = ""
        if AUTO_MARKER in existing_claude:
            existing_auto = existing_claude.split(AUTO_MARKER, 1)[1].strip()

        # Call claude for incremental extraction
        result = call_claude(
            transcript, existing_auto, context_for_llm,
            context_file_name, mem
        )

        if result is None:
            log(mem, "Extraction failed: claude -p returned no result")
            save_state(mem, session_id, message_count)
            return

        # --- Apply CLAUDE.md changes (same as before) ---
        new_auto = result.get("claude_md", "").strip()
        if new_auto:
            merged = merge_claude_md(existing_claude, new_auto)
            atomic_write(claude_md_path, merged)
            log(mem, "Updated CLAUDE.md auto-section")

        # --- Apply incremental context changes ---
        # Let LLM override folder detection if it has a stronger signal
        llm_folder = result.get("primary_folder", "").strip()
        if llm_folder and llm_folder != detected_folder:
            candidate = project_dir / llm_folder
            if candidate.is_dir():
                log(mem, f"LLM suggested folder '{llm_folder}' (overriding detected '{detected_folder}')")
                detected_folder = llm_folder
                context_path = find_context_file(project_dir, detected_folder)
                context_file_name = context_path.name
                # Re-read context for this folder
                existing_context = read_file(context_path)

        additions = result.get("additions", "").strip()
        corrections = result.get("corrections", [])
        removals = result.get("removals", [])
        relevance_notes = result.get("relevance_notes", "")
        summary = result.get("summary", "")

        has_changes = bool(additions or corrections or removals)

        if has_changes:
            # Use the correct existing content for the target path
            base_content = read_file(context_path) if context_path.exists() else ""
            updated = apply_incremental_changes(base_content, additions, corrections, removals)
            atomic_write(context_path, updated)
            log(mem, f"Updated {context_path.relative_to(project_dir)}: "
                      f"+{len(additions)} chars added, "
                      f"{len(corrections)} corrections, "
                      f"{len(removals)} removals")
        else:
            log(mem, f"No context changes needed for {context_file_name}")

        if relevance_notes:
            log(mem, f"Relevance check: {relevance_notes}")

        # Update MEMORY.md index
        memory_md = mem / "MEMORY.md"
        index_content = """# Project Memory Index

Knowledge is automatically extracted from conversations and stored in:
- **CLAUDE.md** (project root): Instructions, workflow rules, things to DO/AVOID
- **Context-<Folder>.md** (in each active folder): Project facts, architecture, business logic

Context files are placed in the folder where work happened (e.g., Context-DB.md in DB/).
Updates are incremental — only new facts are added, wrong facts corrected, stale facts removed.

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
