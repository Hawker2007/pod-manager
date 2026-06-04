"""
file_tools.py — LLM-friendly file operations for small context windows (32k)
Designed for use with local Qwen models via tool/function calling.

Key design principles:
  - Every read operation is token-aware and chunked
  - Line numbers on output so the model can target edits precisely
  - Diff/patch uses unified-diff hunks, not full rewrites
  - Append and patch are surgical — never return more than needed
"""

from __future__ import annotations

import difflib
import hashlib
import json
import math
import os
import re
import tempfile
import time
import uuid
import textwrap
from pathlib import Path
from typing import Any

from fetch_tool import FETCH_MULTIPLE_SCHEMA, FETCH_URL_SCHEMA, fetch_multiple, fetch_url

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Conservative token budget: assume ~3.5 chars/token, keep reads ≤ 6 000 tokens
# (leaving the rest of 32k for prompt + model output)
MAX_READ_CHARS: int = int(6_000 * 3.5)   # ~21 000 chars
CHUNK_SIZE_LINES: int = 200              # lines per page when paginating

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve(path: str) -> Path:
    return Path(path).expanduser().resolve()


def _line_slice(text: str, start: int, end: int | None) -> str:
    """Return lines[start-1 : end] with 1-based inclusive indexing."""
    lines = text.splitlines(keepends=True)
    s = max(0, start - 1)
    e = end if end is not None else len(lines)
    return "".join(lines[s:e])


def _annotate(text: str, start_line: int = 1) -> str:
    """Prefix every line with its 1-based line number (model-friendly)."""
    lines = text.splitlines(keepends=True)
    width = len(str(start_line + len(lines)))
    out = []
    for i, line in enumerate(lines, start=start_line):
        out.append(f"{str(i).rjust(width)}\t{line}")
    return "".join(out)


def _char_budget(text: str) -> dict:
    chars = len(text)
    approx_tokens = math.ceil(chars / 3.5)
    return {"chars": chars, "approx_tokens": approx_tokens}

# ── session storage ─────────────────────────────────────────────────────────
_SESSION_DIR = Path(tempfile.gettempdir()) / "llm_write_sessions"
_SESSION_DIR.mkdir(exist_ok=True)


def _session_path(session_id: str) -> Path:
    return _SESSION_DIR / f"{session_id}.json"


def _load_session(session_id: str) -> dict | None:
    p = _session_path(session_id)
    if not p.exists():
        return None
    return json.loads(p.read_text())


def _save_session(session: dict) -> None:
    _session_path(session["id"]).write_text(json.dumps(session, indent=2))


def _tail_hash(text: str, n_lines: int = 4) -> str:
    """sha1 of the last n lines — used as an anchor the model echoes back."""
    lines = text.splitlines()[-n_lines:]
    return hashlib.sha1("\n".join(lines).encode()).hexdigest()[:12]


# ── validators ──────────────────────────────────────────────────────────────

def _check_html(content: str) -> list[dict]:
    """Detect common HTML structural problems. Returns list of issue dicts."""
    issues = []
    lines = content.splitlines()

    # ── landmark tag counts ────────────────────────────────────────────────
    landmarks = ["html", "head", "body", "title"]
    for tag in landmarks:
        opens  = len(re.findall(rf"<{tag}[\s>]",  content, re.I))
        closes = len(re.findall(rf"</{tag}\s*>", content, re.I))
        if opens > 1:
            issues.append({"kind": "duplicate_open_tag",  "tag": tag, "count": opens})
        if closes > 1:
            issues.append({"kind": "duplicate_close_tag", "tag": tag, "count": closes})
        if opens == 1 and closes == 0:
            issues.append({"kind": "unclosed_tag", "tag": tag})
        if opens == 0 and closes == 1:
            issues.append({"kind": "unopened_close_tag", "tag": tag})

    # ── bracket balance ────────────────────────────────────────────────────
    # (only in <script> blocks to avoid false positives in HTML attributes)
    script_text = " ".join(re.findall(r"<script[^>]*>(.*?)</script>", content, re.S | re.I))
    for ch, close in [("{", "}"), ("(", ")"), ("[", "]")]:
        diff = script_text.count(ch) - script_text.count(close)
        if diff != 0:
            issues.append({
                "kind":  "unbalanced_bracket",
                "open":  ch, "close": close,
                "delta": diff,
                "hint":  "extra opens" if diff > 0 else "extra closes",
            })

    # ── unclosed code fences / block comments ─────────────────────────────
    backtick_fences = len(re.findall(r"^```", content, re.M))
    if backtick_fences % 2 != 0:
        issues.append({"kind": "unclosed_backtick_fence", "count": backtick_fences})

    block_opens  = len(re.findall(r"/\*", content))
    block_closes = len(re.findall(r"\*/", content))
    if block_opens != block_closes:
        issues.append({
            "kind":  "unbalanced_block_comment",
            "opens": block_opens, "closes": block_closes,
        })

    # ── abrupt ending ──────────────────────────────────────────────────────
    last_non_blank = content.rstrip()
    if last_non_blank and not last_non_blank.endswith(">"):
        tail = last_non_blank[-60:]
        if not any(tail.rstrip().endswith(end) for end in [">", "}", ";", "*/", "//", '"""', "'''"]):
            issues.append({"kind": "suspicious_ending", "tail": tail[-40:]})

    return issues


def _check_python(content: str) -> list[dict]:
    """Lightweight Python structural checks."""
    issues = []

    # unmatched triple-quotes
    for q in ['"""', "'''"]:
        if content.count(q) % 2 != 0:
            issues.append({"kind": "unclosed_triple_quote", "quote": q})

    # unmatched brackets (rough)
    for ch, close in [("{", "}"), ("(", ")"), ("[", "]")]:
        diff = content.count(ch) - content.count(close)
        if diff != 0:
            issues.append({"kind": "unbalanced_bracket", "open": ch, "close": close, "delta": diff})

    # obvious truncation: file ends mid-def/class
    lines = content.rstrip().splitlines()
    if lines:
        last = lines[-1].rstrip()
        if last.endswith(":"):
            issues.append({"kind": "trailing_colon", "line": last,
                           "hint": "File ends with a bare colon — likely truncated"})

    return issues


def _check_generic(content: str) -> list[dict]:
    """Checks that apply to any text file."""
    issues = []
    backtick_fences = len(re.findall(r"^```", content, re.M))
    if backtick_fences % 2 != 0:
        issues.append({"kind": "unclosed_backtick_fence", "count": backtick_fences})
    return issues


def _run_validators(path: str, content: str) -> list[dict]:
    ext = Path(path).suffix.lower()
    if ext in {".html", ".htm"}:
        return _check_html(content)
    elif ext in {".py"}:
        return _check_python(content)
    else:
        return _check_generic(content)


def _ok(data: Any = None, **kwargs) -> dict:
    result = {"ok": True}
    if data is not None:
        result["data"] = data
    result.update(kwargs)
    return result


def _err(msg: str) -> dict:
    return {"ok": False, "error": msg}


# ---------------------------------------------------------------------------
# 1. LIST
# ---------------------------------------------------------------------------

def list_files(
    directory: str = ".",
    pattern: str = "*",
    recursive: bool = False,
    max_results: int = 200,
) -> dict:
    """
    List files in a directory, optionally filtered by glob pattern.

    Args:
        directory:   Root directory to list (default: current dir).
        pattern:     Glob pattern, e.g. "**/*.py" or "*.md".
        recursive:   If True, descend into subdirectories.
        max_results: Cap the number of returned entries.

    Returns JSON with:
        files     – list of relative paths
        truncated – True if results were capped
    """
    root = _resolve(directory)
    if not root.is_dir():
        return _err(f"Not a directory: {directory}")

    glob_fn = root.rglob if recursive else root.glob
    entries = []
    truncated = False

    for p in sorted(glob_fn(pattern)):
        if p.is_file():
            rel = str(p.relative_to(root))
            size = p.stat().st_size
            entries.append({"path": rel, "size_bytes": size})
            if len(entries) >= max_results:
                truncated = True
                break

    return _ok(
        files=entries,
        total=len(entries),
        truncated=truncated,
        directory=str(root),
    )


# ---------------------------------------------------------------------------
# 2. READ (with pagination + line ranges)
# ---------------------------------------------------------------------------

def read_file(
    path: str,
    start_line: int = 1,
    end_line: int | None = None,
    annotate: bool = True,
    max_chars: int = MAX_READ_CHARS,
) -> dict:
    """
    Read a file (or a slice of it), respecting the context-window budget.

    Args:
        path:       File to read.
        start_line: First line to return (1-based, inclusive).
        end_line:   Last line to return (1-based, inclusive). None = EOF.
        annotate:   Prefix lines with line numbers (recommended for editing).
        max_chars:  Hard cap on returned characters to protect context window.

    Returns JSON with:
        content       – the (possibly annotated) text
        start_line    – actual first line returned
        end_line      – actual last line returned
        total_lines   – total lines in file
        truncated     – True if content was cut at max_chars
        has_more      – True if lines beyond end_line exist
        budget        – {chars, approx_tokens} of returned content
    """
    p = _resolve(path)
    if not p.is_file():
        return _err(f"File not found: {path}")

    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return _err(str(exc))

    all_lines = text.splitlines(keepends=True)
    total_lines = len(all_lines)
    start_line = max(1, start_line)
    end_line = min(end_line, total_lines) if end_line is not None else total_lines

    chunk = "".join(all_lines[start_line - 1 : end_line])
    truncated = False

    if len(chunk) > max_chars:
        # Trim to the last complete line within budget
        trimmed = chunk[:max_chars]
        last_newline = trimmed.rfind("\n")
        if last_newline != -1:
            trimmed = trimmed[: last_newline + 1]
        chunk = trimmed
        # Recalculate actual end_line
        end_line = start_line + chunk.count("\n") - 1
        truncated = True

    content = _annotate(chunk, start_line) if annotate else chunk

    return _ok(
        content=content,
        start_line=start_line,
        end_line=end_line,
        total_lines=total_lines,
        truncated=truncated,
        has_more=(end_line < total_lines),
        next_start=end_line + 1 if end_line < total_lines else None,
        budget=_char_budget(content),
    )


def read_file_chunk(path: str, page: int = 1, page_size: int = CHUNK_SIZE_LINES) -> dict:
    """
    Convenience wrapper: read a specific page of a file.

    Args:
        path:      File to read.
        page:      1-based page number.
        page_size: Lines per page (default 200).
    """
    start = (page - 1) * page_size + 1
    end = page * page_size
    result = read_file(path, start_line=start, end_line=end)
    if result["ok"]:
        total = result["total_lines"]
        result["page"] = page
        result["total_pages"] = math.ceil(total / page_size)
    return result


# ---------------------------------------------------------------------------
# 3. SEARCH  (grep-style, returns matching lines + context)
# ---------------------------------------------------------------------------

def search_file(
    path: str,
    pattern: str,
    context_lines: int = 2,
    max_matches: int = 40,
    regex: bool = False,
) -> dict:
    """
    Search for a string (or regex) in a file, returning matching lines with context.

    Args:
        path:          File to search.
        pattern:       Search string or regex pattern.
        context_lines: Lines of context before/after each match.
        max_matches:   Maximum number of matches to return.
        regex:         If True, treat pattern as a regular expression.

    Returns JSON with:
        matches – list of {line_no, line, context_before, context_after}
        total_matches
        truncated
    """
    p = _resolve(path)
    if not p.is_file():
        return _err(f"File not found: {path}")

    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    matches = []
    truncated = False

    try:
        rx = re.compile(pattern) if regex else re.compile(re.escape(pattern))
    except re.error as exc:
        return _err(f"Invalid regex: {exc}")

    for i, line in enumerate(lines):
        if rx.search(line):
            before = lines[max(0, i - context_lines) : i]
            after = lines[i + 1 : i + 1 + context_lines]
            matches.append(
                {
                    "line_no": i + 1,
                    "line": line,
                    "context_before": before,
                    "context_after": after,
                }
            )
            if len(matches) >= max_matches:
                truncated = True
                break

    return _ok(matches=matches, total_matches=len(matches), truncated=truncated)


# ---------------------------------------------------------------------------
# 4. WRITE (full file write — only for new or small files)
# ---------------------------------------------------------------------------

def write_file(path: str, content: str, overwrite: bool = False) -> dict:
    """
    Write (or overwrite) a file with the given content.

    Use for new files or small files only.  For large files, prefer patch_file.

    Args:
        path:      Destination path.
        content:   Full file content to write.
        overwrite: Must be True to replace an existing file.

    Returns JSON with lines_written, size_bytes.
    """
    p = _resolve(path)
    if p.exists() and not overwrite:
        return _err(
            f"File already exists: {path}. Set overwrite=True to replace it."
        )
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    except OSError as exc:
        return _err(str(exc))

    return _ok(
        path=str(p),
        lines_written=content.count("\n") + (1 if not content.endswith("\n") else 0),
        size_bytes=p.stat().st_size,
    )


# ---------------------------------------------------------------------------
# 5. APPEND
# ---------------------------------------------------------------------------

def append_file(path: str, content: str, newline_before: bool = True) -> dict:
    """
    Append text to the end of a file (creates the file if it doesn't exist).

    Args:
        path:           Target file.
        content:        Text to append.
        newline_before: Ensure a newline separates existing content from appended text.

    Returns JSON with new total_lines, size_bytes.
    """
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    try:
        if newline_before and p.exists() and p.stat().st_size > 0:
            existing_tail = p.read_bytes()[-1:]
            if existing_tail != b"\n":
                content = "\n" + content

        with p.open("a", encoding="utf-8") as f:
            f.write(content)
    except OSError as exc:
        return _err(str(exc))

    total_lines = sum(1 for _ in p.open(encoding="utf-8"))
    return _ok(path=str(p), total_lines=total_lines, size_bytes=p.stat().st_size)


# ---------------------------------------------------------------------------
# 6. PATCH  (surgical line-range replacement — context-window friendly)
# ---------------------------------------------------------------------------

def patch_file(
    path: str,
    start_line: int,
    end_line: int,
    new_content: str,
    dry_run: bool = False,
) -> dict:
    """
    Replace lines [start_line, end_line] (inclusive, 1-based) with new_content.

    This is the primary editing tool for large files — send only the changed
    region, not the whole file.

    Args:
        path:        File to edit.
        start_line:  First line to replace (1-based).
        end_line:    Last line to replace (1-based).
        new_content: Replacement text (must end with newline, or one is added).
        dry_run:     If True, return the diff without writing.

    Returns JSON with:
        lines_removed, lines_added, diff (unified-diff snippet), path
    """
    p = _resolve(path)
    if not p.is_file():
        return _err(f"File not found: {path}")

    original = p.read_text(encoding="utf-8", errors="replace")
    lines = original.splitlines(keepends=True)
    total = len(lines)

    if start_line < 1 or start_line > total + 1:
        return _err(f"start_line {start_line} out of range (file has {total} lines)")
    end_line = min(end_line, total)

    if new_content and not new_content.endswith("\n"):
        new_content += "\n"

    before = lines[: start_line - 1]
    after = lines[end_line:]
    new_lines = new_content.splitlines(keepends=True)
    patched_lines = before + new_lines + after
    patched = "".join(patched_lines)

    diff_lines = list(
        difflib.unified_diff(
            lines[start_line - 1 : end_line],
            new_lines,
            fromfile=f"{path} (original lines {start_line}-{end_line})",
            tofile=f"{path} (patched)",
            lineterm="",
        )
    )
    diff_text = "\n".join(diff_lines)

    if not dry_run:
        try:
            p.write_text(patched, encoding="utf-8")
        except OSError as exc:
            return _err(str(exc))

    return _ok(
        path=str(p),
        lines_removed=end_line - start_line + 1,
        lines_added=len(new_lines),
        new_total_lines=len(patched_lines),
        diff=diff_text,
        dry_run=dry_run,
    )


# ────────────────────────────────────────────────────────────────────────────
# Chunked write + validation tools
# ────────────────────────────────────────────────────────────────────────────

def write_chunk_begin(
    path: str,
    expected_lines: int | None = None,
    overwrite: bool = False,
) -> dict:
    """
    Open a new chunked-write session for a file.

    Call this BEFORE the first write_chunk. It creates an empty target file
    and registers a session that tracks progress.

    Args:
        path:           Destination file path.
        expected_lines: How many lines you plan to write in total (optional
                        but strongly recommended — enables completion checking).
        overwrite:      Set True to replace an existing file.

    Returns JSON with:
        session_id    – pass this to every write_chunk / write_chunk_commit
        path          – resolved absolute path
        expected_lines
    """
    p = _resolve(path)
    if p.exists() and not overwrite:
        return _err(f"File already exists: {path}. Set overwrite=True.")

    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("", encoding="utf-8")

    session = {
        "id":             str(uuid.uuid4())[:8],
        "path":           str(p),
        "expected_lines": expected_lines,
        "chunks_written": 0,
        "lines_written":  0,
        "tail_hash":      None,
        "created_at":     time.time(),
    }
    _save_session(session)

    return _ok(
        session_id=session["id"],
        path=str(p),
        expected_lines=expected_lines,
        message="Session open. Call write_chunk(session_id, content) to add content.",
    )


def write_chunk(
    session_id: str,
    content: str,
    expected_tail_hash: str | None = None,
) -> dict:
    """
    Append a chunk of content to the active write session.

    Args:
        session_id:         From write_chunk_begin.
        content:            The text chunk to append.
        expected_tail_hash: (Optional) Echo back the tail_hash from the
                            previous write_chunk call to verify the join
                            point is intact.

    Returns JSON with:
        chunk_index       – which chunk this was (1-based)
        lines_this_chunk  – lines in this chunk
        total_lines_so_far
        tail_hash         – sha1 of last 4 lines of file (echo in next call)
        join_ok           – True if the join with previous chunk looks clean
        overlap_warning   – description if join looks suspect
    """
    session = _load_session(session_id)
    if session is None:
        return _err(f"Unknown session: {session_id}. Call write_chunk_begin first.")

    p = Path(session["path"])
    join_ok = True
    overlap_warning = None

    # ── validate expected tail hash ───────────────────────────────────────
    if expected_tail_hash and session["tail_hash"]:
        if expected_tail_hash != session["tail_hash"]:
            return _err(
                f"tail_hash mismatch: you sent '{expected_tail_hash}' "
                f"but file tail is '{session['tail_hash']}'. "
                "Content may have been lost or duplicated between chunks. "
                "Call read_file to inspect the join point before continuing."
            )

    # ── check join quality ────────────────────────────────────────────────
    if session["chunks_written"] > 0 and p.exists() and p.stat().st_size > 0:
        existing_tail = p.read_text(encoding="utf-8")[-200:]
        chunk_head    = content[:200]

        # detect duplicated lines at boundary
        tail_lines  = existing_tail.rstrip().splitlines()[-3:]
        chunk_lines = chunk_head.lstrip().splitlines()[:3]
        overlap = set(tail_lines) & set(chunk_lines)
        if overlap and any(len(l.strip()) > 10 for l in overlap):
            join_ok = False
            overlap_warning = f"Possible duplicate lines at join: {list(overlap)[:2]}"

        # ensure newline separation
        if existing_tail and not existing_tail.endswith("\n"):
            content = "\n" + content

    # ── write ─────────────────────────────────────────────────────────────
    try:
        with p.open("a", encoding="utf-8") as f:
            f.write(content)
    except OSError as exc:
        return _err(str(exc))

    # ── update session ────────────────────────────────────────────────────
    chunk_lines   = content.count("\n") + (0 if content.endswith("\n") else 1)
    new_total     = sum(1 for _ in p.open(encoding="utf-8"))
    new_tail_hash = _tail_hash(p.read_text(encoding="utf-8"))

    session["chunks_written"] += 1
    session["lines_written"]   = new_total
    session["tail_hash"]       = new_tail_hash
    _save_session(session)

    result = _ok(
        chunk_index=session["chunks_written"],
        lines_this_chunk=chunk_lines,
        total_lines_so_far=new_total,
        tail_hash=new_tail_hash,
        join_ok=join_ok,
    )
    if overlap_warning:
        result["overlap_warning"] = overlap_warning

    if session["expected_lines"]:
        pct = round(new_total / session["expected_lines"] * 100)
        result["progress"] = f"{new_total}/{session['expected_lines']} lines ({pct}%)"

    return result


def write_chunk_commit(session_id: str) -> dict:
    """
    Finalize a chunked write session and run full integrity validation.

    Args:
        session_id: From write_chunk_begin.

    Returns JSON with:
        path
        total_lines
        expected_lines
        lines_match        – True if within ±2% of expected
        sha256             – fingerprint of final file
        issues             – list of structural problems found (empty = clean)
        verdict            – "ok" | "warnings" | "needs_repair"
        repair_hint        – guidance if problems were found
    """
    session = _load_session(session_id)
    if session is None:
        return _err(f"Unknown session: {session_id}")

    p = Path(session["path"])
    if not p.exists():
        return _err(f"Target file missing: {session['path']}")

    content = p.read_text(encoding="utf-8", errors="replace")
    total_lines = content.count("\n") + (1 if not content.endswith("\n") else 0)
    sha256 = hashlib.sha256(content.encode()).hexdigest()

    # ── line count check ──────────────────────────────────────────────────
    expected = session.get("expected_lines")
    lines_match = True
    line_delta = None
    if expected:
        line_delta  = total_lines - expected
        lines_match = abs(line_delta) <= max(2, int(expected * 0.02))  # 2% tolerance

    # ── structural validation ─────────────────────────────────────────────
    issues = _run_validators(str(p), content)

    # ── verdict ───────────────────────────────────────────────────────────
    if not issues and lines_match:
        verdict = "ok"
        repair_hint = None
    elif issues:
        verdict = "needs_repair"
        repair_hint = (
            "Use search_file to locate the problematic regions, "
            "then patch_file to fix them. "
            "Issues list includes kind, tag/bracket, and hints."
        )
    else:
        verdict = "warnings"
        repair_hint = (
            f"Line count is off by {line_delta:+d}. "
            "Read the file tail to check for truncation or duplication."
        )

    # ── clean up session ──────────────────────────────────────────────────
    _session_path(session_id).unlink(missing_ok=True)

    return _ok(
        path=str(p),
        total_lines=total_lines,
        expected_lines=expected,
        line_delta=line_delta,
        lines_match=lines_match,
        chunks_used=session["chunks_written"],
        sha256=sha256,
        issues=issues,
        verdict=verdict,
        repair_hint=repair_hint,
    )


def validate_file(path: str, expected_lines: int | None = None) -> dict:
    """
    Run structural validation on any existing file.

    Use after patch_file repairs to confirm the file is now clean,
    or as a standalone check on any generated file.

    Args:
        path:           File to validate.
        expected_lines: Optional line count expectation.

    Returns the same schema as write_chunk_commit.
    """
    p = _resolve(path)
    if not p.is_file():
        return _err(f"File not found: {path}")

    content = p.read_text(encoding="utf-8", errors="replace")
    total_lines = content.count("\n") + (1 if not content.endswith("\n") else 0)
    sha256 = hashlib.sha256(content.encode()).hexdigest()

    lines_match = True
    line_delta  = None
    if expected_lines:
        line_delta  = total_lines - expected_lines
        lines_match = abs(line_delta) <= max(2, int(expected_lines * 0.02))

    issues = _run_validators(str(p), content)

    if not issues and lines_match:
        verdict = "ok"
        repair_hint = None
    elif issues:
        verdict = "needs_repair"
        repair_hint = (
            "Use search_file to locate the problematic regions, "
            "then patch_file to fix them."
        )
    else:
        verdict = "warnings"
        repair_hint = f"Line count off by {line_delta:+d}."

    return _ok(
        path=str(p),
        total_lines=total_lines,
        expected_lines=expected_lines,
        line_delta=line_delta,
        lines_match=lines_match,
        sha256=sha256,
        issues=issues,
        verdict=verdict,
        repair_hint=repair_hint,
    )


# ---------------------------------------------------------------------------
# 7. DIFF  (compare two files or two versions)
# ---------------------------------------------------------------------------

def diff_files(
    path_a: str,
    path_b: str,
    context_lines: int = 3,
    max_diff_chars: int = MAX_READ_CHARS,
) -> dict:
    """
    Produce a unified diff between two files.

    Args:
        path_a:         Original file.
        path_b:         Modified file.
        context_lines:  Lines of context around each hunk.
        max_diff_chars: Truncate diff output to protect context window.

    Returns JSON with:
        diff      – unified diff text
        truncated – True if diff was cut
        budget    – {chars, approx_tokens}
    """
    pa, pb = _resolve(path_a), _resolve(path_b)
    for fp, label in [(pa, path_a), (pb, path_b)]:
        if not fp.is_file():
            return _err(f"File not found: {label}")

    lines_a = pa.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    lines_b = pb.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)

    diff_iter = difflib.unified_diff(
        lines_a, lines_b,
        fromfile=path_a, tofile=path_b,
        n=context_lines,
        lineterm="",
    )
    diff_text = "\n".join(diff_iter)
    truncated = False

    if len(diff_text) > max_diff_chars:
        diff_text = diff_text[:max_diff_chars]
        last_nl = diff_text.rfind("\n")
        if last_nl != -1:
            diff_text = diff_text[:last_nl]
        truncated = True

    return _ok(diff=diff_text, truncated=truncated, budget=_char_budget(diff_text))


# ---------------------------------------------------------------------------
# 8. FILE INFO  (metadata without reading content)
# ---------------------------------------------------------------------------

def file_info(path: str) -> dict:
    """
    Return metadata about a file: size, line count, language hint, etc.
    Safe to call on large files — reads only what's needed.

    Args:
        path: File to inspect.

    Returns JSON with size_bytes, total_lines, extension, encoding_hint.
    """
    p = _resolve(path)
    if not p.exists():
        return _err(f"Path not found: {path}")

    stat = p.stat()
    ext = p.suffix.lower()
    total_lines = 0

    if p.is_file():
        try:
            with p.open("rb") as f:
                for _ in f:
                    total_lines += 1
        except OSError:
            pass

    fits_in_context = stat.st_size <= MAX_READ_CHARS
    pages_needed = math.ceil(total_lines / CHUNK_SIZE_LINES)

    return _ok(
        path=str(p),
        size_bytes=stat.st_size,
        total_lines=total_lines,
        extension=ext,
        fits_in_context=fits_in_context,
        pages_needed=pages_needed if not fits_in_context else 1,
        is_directory=p.is_dir(),
    )


# ---------------------------------------------------------------------------
# 9. FIND_SYMBOL  (quick code navigation without AST)
# ---------------------------------------------------------------------------

def find_symbol(
    path: str,
    symbol: str,
    kind: str = "any",
) -> dict:
    """
    Find function/class/variable definitions in a source file by name.

    Args:
        path:   Source file to search.
        symbol: Symbol name to look for.
        kind:   One of "function", "class", "variable", "any".

    Returns JSON with definitions list: [{line_no, line, kind}]
    """
    p = _resolve(path)
    if not p.is_file():
        return _err(f"File not found: {path}")

    patterns: dict[str, str] = {
        "function": rf"^\s*(?:async\s+)?def\s+{re.escape(symbol)}\s*[\(:]",
        "class":    rf"^\s*class\s+{re.escape(symbol)}\s*[\(:]",
        "variable": rf"^\s*{re.escape(symbol)}\s*=",
    }

    if kind == "any":
        combined = "|".join(f"({v})" for v in patterns.values())
        rx = re.compile(combined)
    elif kind in patterns:
        rx = re.compile(patterns[kind])
    else:
        return _err(f"Unknown kind '{kind}'. Use: function, class, variable, any")

    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    hits = []
    for i, line in enumerate(lines, 1):
        m = rx.match(line)
        if m:
            detected_kind = "unknown"
            for k, pat in patterns.items():
                if re.match(pat, line):
                    detected_kind = k
                    break
            hits.append({"line_no": i, "line": line.rstrip(), "kind": detected_kind})

    return _ok(definitions=hits, total=len(hits), symbol=symbol)


# ---------------------------------------------------------------------------
# Tool registry (for Qwen/Ollama function-calling JSON schema)
# ---------------------------------------------------------------------------

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files in a directory. Use before reading to understand structure.",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {"type": "string", "default": "."},
                    "pattern":   {"type": "string", "default": "*", "description": "Glob pattern e.g. '**/*.py'"},
                    "recursive": {"type": "boolean", "default": False},
                    "max_results": {"type": "integer", "default": 200},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "file_info",
            "description": (
                "Get file metadata (size, line count, pages needed) without reading content. "
                "Always call this first on unknown files to decide whether to read all at once or paginate."
            ),
            "parameters": {
                "type": "object",
                "required": ["path"],
                "properties": {"path": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a file or a line range. Use start_line/end_line to paginate large files. "
                "Lines are annotated with numbers so you can target patches precisely."
            ),
            "parameters": {
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path":       {"type": "string"},
                    "start_line": {"type": "integer", "default": 1},
                    "end_line":   {"type": "integer", "description": "Omit for EOF"},
                    "annotate":   {"type": "boolean", "default": True},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file_chunk",
            "description": "Read a specific page of a large file (200 lines per page by default).",
            "parameters": {
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path":      {"type": "string"},
                    "page":      {"type": "integer", "default": 1},
                    "page_size": {"type": "integer", "default": 200},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_file",
            "description": (
                "Grep a file for a string or regex. Returns matching lines + context. "
                "Use to locate code before patching without reading the whole file."
            ),
            "parameters": {
                "type": "object",
                "required": ["path", "pattern"],
                "properties": {
                    "path":          {"type": "string"},
                    "pattern":       {"type": "string"},
                    "context_lines": {"type": "integer", "default": 2},
                    "max_matches":   {"type": "integer", "default": 40},
                    "regex":         {"type": "boolean", "default": False},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_symbol",
            "description": (
                "Find where a function, class, or variable is defined in a source file. "
                "Returns line numbers so you can read_file with start_line/end_line."
            ),
            "parameters": {
                "type": "object",
                "required": ["path", "symbol"],
                "properties": {
                    "path":   {"type": "string"},
                    "symbol": {"type": "string"},
                    "kind":   {"type": "string", "enum": ["function", "class", "variable", "any"], "default": "any"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write a complete file. Use only for new or small files. For edits to large files, use patch_file.",
            "parameters": {
                "type": "object",
                "required": ["path", "content"],
                "properties": {
                    "path":      {"type": "string"},
                    "content":   {"type": "string"},
                    "overwrite": {"type": "boolean", "default": False},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_file",
            "description": "Append text to a file (creates it if missing). Good for logs, adding new functions at end of file.",
            "parameters": {
                "type": "object",
                "required": ["path", "content"],
                "properties": {
                    "path":           {"type": "string"},
                    "content":        {"type": "string"},
                    "newline_before": {"type": "boolean", "default": True},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "patch_file",
            "description": (
                "Replace a range of lines in a file. The primary tool for editing large files. "
                "Use search_file or find_symbol to locate the lines first, then replace only the changed region."
            ),
            "parameters": {
                "type": "object",
                "required": ["path", "start_line", "end_line", "new_content"],
                "properties": {
                    "path":        {"type": "string"},
                    "start_line":  {"type": "integer"},
                    "end_line":    {"type": "integer"},
                    "new_content": {"type": "string"},
                    "dry_run":     {"type": "boolean", "default": False},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "diff_files",
            "description": "Produce a unified diff between two files. Useful for reviewing changes before committing.",
            "parameters": {
                "type": "object",
                "required": ["path_a", "path_b"],
                "properties": {
                    "path_a":        {"type": "string"},
                    "path_b":        {"type": "string"},
                    "context_lines": {"type": "integer", "default": 3},
                },
            },
        },
    },
    {
      "type": "function",
      "function": {
        "name": "write_chunk_begin",
        "description": "Open a chunked-write session before generating a large file. Call ONCE before the first write_chunk. Declare expected_lines if you know roughly how long the file will be.",
        "parameters": {
          "type": "object",
          "required": ["path"],
          "properties": {
            "path": {"type": "string"},
            "expected_lines": {"type": "integer", "description": "Estimated total lines"},
            "overwrite": {"type": "boolean", "default": False},
          },
        },
      },
    },
    {
      "type": "function",
      "function": {
        "name": "write_chunk",
        "description": "Append one chunk of generated content to the active write session. Each chunk should be 150-200 lines. Echo expected_tail_hash from the previous call to verify the join point.",
        "parameters": {
          "type": "object",
          "required": ["session_id", "content"],
          "properties": {
            "session_id": {"type": "string"},
            "content": {"type": "string"},
            "expected_tail_hash": {"type": "string", "description": "Echo the tail_hash returned by the previous write_chunk to verify join integrity."},
          },
        },
      },
    },
    {
      "type": "function",
      "function": {
        "name": "write_chunk_commit",
        "description": "Finalize a write session and validate the completed file. Call ONCE after all write_chunk calls. Returns verdict: ok | warnings | needs_repair, plus a list of issues.",
        "parameters": {
          "type": "object",
          "required": ["session_id"],
          "properties": {
            "session_id": {"type": "string"},
          },
        },
      },
    },
    {
      "type": "function",
      "function": {
        "name": "validate_file",
        "description": "Run structural validation on any existing file. Use after patch_file repairs to confirm the file is clean.",
        "parameters": {
          "type": "object",
          "required": ["path"],
          "properties": {
            "path": {"type": "string"},
            "expected_lines": {"type": "integer"},
          },
        },
      },
    },
    FETCH_URL_SCHEMA,
    FETCH_MULTIPLE_SCHEMA,
]


TOOL_MAP: dict[str, Any] = {
    "list_files":     list_files,
    "file_info":      file_info,
    "read_file":      read_file,
    "read_file_chunk": read_file_chunk,
    "search_file":    search_file,
    "find_symbol":    find_symbol,
    "write_file":     write_file,
    "fetch_url":      fetch_url,
    "fetch_multiple": fetch_multiple,
    "append_file":    append_file,
    "patch_file":     patch_file,
    "diff_files":     diff_files,
    "write_chunk_begin": write_chunk_begin,
    "write_chunk":       write_chunk,
    "write_chunk_commit": write_chunk_commit,
    "validate_file":     validate_file,
}


def dispatch(tool_name: str, arguments: dict | str) -> dict | list[dict]:
    """
    Execute a tool by name with the given arguments dict (or JSON string).
    This is the single entry point for the LLM loop.
    """
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as exc:
            return _err(f"Invalid JSON arguments: {exc}")

    fn = TOOL_MAP.get(tool_name)
    if fn is None:
        return _err(f"Unknown tool: {tool_name}. Available: {list(TOOL_MAP)}")

    try:
        result = fn(**arguments)
        
        # Convert FetchResult objects to dicts for fetch_url and fetch_multiple
        if tool_name == "fetch_url":
            return result.to_dict()
        elif tool_name == "fetch_multiple":
            return [r.to_dict() for r in result]
        
        return result
    except TypeError as exc:
        return _err(f"Bad arguments for {tool_name}: {exc}")


if __name__ == "__main__":
    # Quick smoke-test
    import tempfile, os

    with tempfile.TemporaryDirectory() as td:
        sample = os.path.join(td, "sample.py")

        # Write
        r = write_file(sample, "def hello():\n    print('hello')\n\ndef world():\n    print('world')\n")
        assert r["ok"], r

        # Info
        r = file_info(sample)
        assert r["total_lines"] == 5, r

        # Read
        r = read_file(sample)
        assert "hello" in r["content"]

        # Search
        r = search_file(sample, "def ")
        assert r["total_matches"] == 2, r

        # find_symbol
        r = find_symbol(sample, "hello")
        assert r["definitions"][0]["line_no"] == 1

        # Patch
        r = patch_file(sample, 1, 2, "def hello(name='world'):\n    print(f'hello {name}')\n")
        assert r["ok"], r

        # Verify patch
        r = read_file(sample, annotate=False)
        assert "hello(name" in r["content"], r["content"]

        # Diff
        sample2 = os.path.join(td, "sample2.py")
        write_file(sample2, "def hello():\n    print('hi')\n")
        r = diff_files(sample, sample2)
        assert r["ok"] and "@@" in r["diff"]

        # Append
        r = append_file(sample, "\n# end of file\n")
        assert r["ok"]

        print("All smoke tests passed")
        print(f"\nAvailable tools: {list(TOOL_MAP)}")
