#!/usr/bin/env python3
"""
Knowledge Extractor — Stop hook for Claude Code.

Parses the session transcript, pipes it to `claude -p --model <model>` to extract
knowledge INCREMENTALLY, then applies targeted edits to:
  - .claude/rules/<topic>.md  (scoped instructions with YAML frontmatter path filters)
  - Context-<Folder>.md (project context placed in the primary working folder)

Key behaviors:
  - INCREMENTAL: Only adds new facts, corrects wrong facts, removes stale facts.
    Never rewrites the entire context file.
  - RULES-BASED: Instructions go to .claude/rules/ files with optional path scoping,
    NOT to CLAUDE.md (which stays manual-only).
  - FOLDER-LEVEL: Detects the primary working folder from file paths in the
    transcript and places the context file there (e.g., Context-DB.md in DB/).

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
    root = project_root.rstrip("/")
    path_pattern = re.compile(re.escape(root) + r'/([^/\s"\'<>]+)')
    matches = path_pattern.findall(transcript)

    if not matches:
        return ""

    folder_counts = {}
    for match in matches:
        folder = match.split("/")[0] if "/" in match else match
        if "." in folder and not folder.startswith("."):
            continue
        if folder.startswith("."):
            continue
        folder_counts[folder] = folder_counts.get(folder, 0) + 1

    if not folder_counts:
        return ""

    primary = max(folder_counts, key=folder_counts.get)

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
# Rules file helpers
# ---------------------------------------------------------------------------

def rules_dir(project_dir: Path) -> Path:
    return project_dir / ".claude" / "rules"


def read_existing_rules(project_dir: Path) -> dict:
    """
    Read all existing .claude/rules/*.md files.
    Returns {filename_without_ext: {"path": Path, "content": str, "description": str, "paths": list}}
    """
    rd = rules_dir(project_dir)
    rules = {}
    if not rd.is_dir():
        return rules

    for f in rd.iterdir():
        if f.suffix == ".md" and f.name != "Icon":
            content = read_file(f)
            name = f.stem  # e.g., "pgm", "gws", "trashformers"
            # Parse frontmatter for description and paths
            desc = ""
            paths = []
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    fm = parts[1]
                    for line in fm.strip().splitlines():
                        line = line.strip()
                        if line.startswith("description:"):
                            desc = line.split(":", 1)[1].strip().strip('"').strip("'")
                        if line.startswith("- "):
                            # path entry under paths:
                            paths.append(line[2:].strip().strip('"').strip("'"))
            rules[name] = {
                "path": f,
                "content": content,
                "description": desc,
                "paths": paths,
            }
    return rules


def build_rules_summary(rules: dict) -> str:
    """Build a summary of existing rules files for the LLM prompt."""
    if not rules:
        return "(no rules files yet)"

    lines = []
    for name, info in sorted(rules.items()):
        desc = info["description"] or "(no description)"
        path_scope = ", ".join(info["paths"]) if info["paths"] else "(global)"
        # Count instruction lines (lines starting with "- ")
        instruction_count = sum(1 for l in info["content"].splitlines() if l.strip().startswith("- "))
        lines.append(f"  - {name}.md: {desc} | scope: {path_scope} | {instruction_count} instructions")
    return "\n".join(lines)


def write_rules_file(project_dir: Path, name: str, description: str,
                     path_scope: list, instructions: str, mem: Path):
    """Write or update a .claude/rules/<name>.md file."""
    rd = rules_dir(project_dir)
    rd.mkdir(parents=True, exist_ok=True)
    target = rd / f"{name}.md"

    # Build frontmatter
    fm_lines = ["---"]
    fm_lines.append(f'description: "{description}"')
    if path_scope:
        fm_lines.append("paths:")
        for p in path_scope:
            fm_lines.append(f'  - "{p}"')
    fm_lines.append("---")

    content = "\n".join(fm_lines) + "\n" + instructions.strip() + "\n"
    atomic_write(target, content)
    log(mem, f"Updated rules file: .claude/rules/{name}.md ({len(instructions.splitlines())} lines)")


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
        lines = result.split("\n")
        new_lines = []
        for line in lines:
            if removal in line:
                continue
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
Your job: extract NEW knowledge and produce INCREMENTAL updates.

You produce TWO types of output:

OUTPUT 1 — rules (Instructions for .claude/rules/ files):
Things the AI assistant should DO or AVOID. These go into SCOPED rules files
in .claude/rules/<topic>.md with optional path filters.

Each rules file has:
  - A short kebab-case name (e.g., "pgm", "gws", "trashformers", "vis-analysis")
  - A one-line description
  - Optional path scope (glob patterns like "Trashformers/**" or "ProgramManagement/**")
  - Instruction lines in imperative voice: "- Always X", "- Never Y"

CRITICAL RULES for rules output:
  - Check the existing rules files listed below. If the new instruction fits an
    EXISTING file, add it there (return that file's name). Do NOT create a new file
    for something that already has a file.
  - If the instruction is genuinely new topic, create a new file.
  - Return the FULL updated content of the rules file (all instructions, not just new ones)
    because rules files are replaced atomically.
  - Remove exact duplicates. If a new instruction contradicts an old one, keep the new one.
  - If no new instructions were learned, return an empty rules array.

OUTPUT 2 — context changes (Incremental):
Same as before — additions/corrections/removals for the Context-<Folder>.md file.

RULES:
- Only extract CONFIRMED knowledge (stated by user or verified by actions)
- Only extract REUSABLE knowledge (skip one-off debugging details)
- Be CONSERVATIVE with corrections and removals — only when clearly wrong
- Be concise but thorough"""


def call_claude(transcript: str, rules_summary: str, existing_rules: dict,
                existing_context: str, context_file_name: str, mem: Path) -> dict:
    """Call claude -p with the extraction prompt, return parsed JSON."""

    # Build detailed existing rules content for the LLM
    rules_detail = ""
    for name, info in sorted(existing_rules.items()):
        # Extract just the instruction lines (after frontmatter)
        content = info["content"]
        if "---" in content:
            parts = content.split("---", 2)
            if len(parts) >= 3:
                instructions_part = parts[2].strip()
            else:
                instructions_part = content
        else:
            instructions_part = content
        rules_detail += f"\n### {name}.md (scope: {', '.join(info['paths']) or 'global'})\n{instructions_part}\n"

    user_prompt = f"""{SYSTEM_PROMPT}

---

Here is the conversation transcript from a Claude Code session:

<transcript>
{transcript}
</transcript>

Here are the existing .claude/rules/ files:
<existing_rules>
{rules_summary}

Detailed content:
{rules_detail if rules_detail.strip() else "(no rules files yet)"}
</existing_rules>

Here is the existing context file ({context_file_name}):
<existing_context>
{existing_context if existing_context else "(none yet — this is a new context file)"}
</existing_context>

Analyze the transcript. Produce incremental updates.
If nothing new was discussed, return empty arrays.

You MUST respond with ONLY a JSON object (no markdown fences, no extra text) with exactly these keys:

- "rules": array of objects, each with:
    - "file": string — kebab-case filename without extension (e.g., "pgm", "gws", "vis-analysis")
    - "description": string — one-line description for YAML frontmatter
    - "path_scope": array of glob strings (e.g., ["Trashformers/**"]), or [] for global
    - "instructions": string — FULL content of the rules file (all instruction lines, not just new ones)
  Return [] if no new instructions.

- "primary_folder": string — subfolder where most work happened, or "" for root
- "additions": string — new markdown for context file, or ""
- "corrections": array of {{"find": "exact old text", "replace": "new text"}}, or []
- "removals": array of strings, or []
- "relevance_notes": string
- "summary": string — 1-2 sentence summary

Example:
{{"rules": [{{"file": "vis-analysis", "description": "VIS print inspection analysis rules", "path_scope": ["VISAnanlysis/**"], "instructions": "- When classifying VIS failures, distinguish warp from missing print\\n- Floor stop rules: same track fails 2+ consecutive rows = STOP"}}], "primary_folder": "VISAnanlysis", "additions": "", "corrections": [], "removals": [], "relevance_notes": "Checked existing rules", "summary": "Added 2 VIS rules"}}"""

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

        # Normalize rules to list
        val = parsed.get("rules", [])
        if not isinstance(val, list):
            parsed["rules"] = []

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
        legacy_context_path = project_dir / "context.md"
        legacy_context = ""
        if not existing_context and legacy_context_path.exists() and detected_folder:
            legacy_context = read_file(legacy_context_path)
            if legacy_context:
                log(mem, f"Found legacy context.md at root, will consider it")

        context_for_llm = existing_context or legacy_context

        # Read existing .claude/rules/ files
        existing_rules = read_existing_rules(project_dir)
        rules_summary = build_rules_summary(existing_rules)
        log(mem, f"Found {len(existing_rules)} existing rules files")

        # Call claude for extraction
        result = call_claude(
            transcript, rules_summary, existing_rules,
            context_for_llm, context_file_name, mem
        )

        if result is None:
            log(mem, "Extraction failed: claude -p returned no result")
            save_state(mem, session_id, message_count)
            return

        # --- Apply rules file changes ---
        rules_updates = result.get("rules", [])
        for rule in rules_updates:
            if not isinstance(rule, dict):
                continue
            file_name = rule.get("file", "").strip()
            if not file_name:
                continue
            # Sanitize filename
            file_name = re.sub(r'[^a-z0-9\-]', '-', file_name.lower())
            description = rule.get("description", "").strip()
            path_scope = rule.get("path_scope", [])
            instructions = rule.get("instructions", "").strip()
            if not instructions:
                continue
            if not isinstance(path_scope, list):
                path_scope = []
            write_rules_file(project_dir, file_name, description, path_scope,
                             instructions, mem)

        # --- Apply incremental context changes ---
        llm_folder = result.get("primary_folder", "").strip()
        if llm_folder and llm_folder != detected_folder:
            candidate = project_dir / llm_folder
            if candidate.is_dir():
                log(mem, f"LLM suggested folder '{llm_folder}' (overriding detected '{detected_folder}')")
                detected_folder = llm_folder
                context_path = find_context_file(project_dir, detected_folder)
                context_file_name = context_path.name
                existing_context = read_file(context_path)

        additions = result.get("additions", "").strip()
        corrections = result.get("corrections", [])
        removals = result.get("removals", [])
        relevance_notes = result.get("relevance_notes", "")
        summary = result.get("summary", "")

        has_changes = bool(additions or corrections or removals)

        if has_changes:
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
- **.claude/rules/<topic>.md**: Scoped instructions with optional path filters
- **Context-<Folder>.md** (in each active folder): Project facts, architecture, business logic

Rules files are path-scoped (e.g., pgm.md only loads when working in ProgramManagement/).
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
