#!/usr/bin/env python3
"""
update_index.py — Gemma-powered Semantic Index updater.

Scans your source directory for code files, compares modification times against
their corresponding SEMANTIC_INDEX/ entries, and uses a local Ollama model to
regenerate stale or missing index files.

Usage:
    # Update all stale entries (safe to run any time)
    python3 update_index.py

    # Force re-index specific files
    python3 update_index.py --force --filter player

    # Preview what would be updated
    python3 update_index.py --dry-run

    # Re-index everything from scratch
    python3 update_index.py --force --all
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error


# ---------------------------------------------------------------------------
# Config — EDIT THESE FOR YOUR PROJECT
# ---------------------------------------------------------------------------

# Root of your project (parent of src/ and SEMANTIC_INDEX/)
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "."))

# Where your source code lives
SRC_DIR = os.path.join(PROJECT_ROOT, "src")

# Where index entries are written
INDEX_DIR = os.path.join(PROJECT_ROOT, "SEMANTIC_INDEX")

# Ollama endpoint (default local)
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/generate")

# Model to use — override with OLLAMA_MODEL env var
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma3:12b")

# Timeout per model call (seconds)
TIMEOUT = int(os.environ.get("INDEX_TIMEOUT", "180"))

# File extensions to index
SOURCE_EXTS = {".py", ".js", ".ts", ".tsx", ".jsx",
               ".cpp", ".c", ".h", ".hpp", ".cc",
               ".go", ".rs", ".java", ".kt", ".swift",
               ".rb", ".lua", ".zig", ".odin",
               ".cs", ".mm", ".metal", ".wgsl", ".glsl"}

# Subdirectories within SRC_DIR to scan (empty = scan all)
# Example: ["engine", "game", "editor"]
SCAN_DIRS = []

# Files/patterns to skip (vendored code, generated files, etc.)
# Matched against the relative path from SRC_DIR
SKIP_PATTERNS = [
    # Example: "vendor/", "node_modules/", "stb_image.h"
]

# Max chars to send to the model in a single call
MAX_INPUT_CHARS = 350_000

# Files above this size get chunked into multiple calls
CHUNK_TARGET_CHARS = 40_000


# ---------------------------------------------------------------------------
# Path mapping: src/game/player.cpp -> SEMANTIC_INDEX/game/player_cpp.md
# ---------------------------------------------------------------------------

def src_to_index_path(src_path):
    """Convert a source file path to its Semantic Index path."""
    rel = os.path.relpath(src_path, SRC_DIR)
    base, ext = os.path.splitext(rel)
    ext_slug = ext.lstrip(".").replace("+", "p")
    index_name = f"{base}_{ext_slug}.md"
    return os.path.join(INDEX_DIR, index_name)


def index_relative_path(src_path):
    """Get the relative path as it appears in index headers."""
    return os.path.relpath(src_path, SRC_DIR)


# ---------------------------------------------------------------------------
# Staleness detection
# ---------------------------------------------------------------------------

def find_source_files():
    """Find all indexable source files under SRC_DIR."""
    sources = []

    if SCAN_DIRS:
        dirs_to_walk = [os.path.join(SRC_DIR, d) for d in SCAN_DIRS]
    else:
        dirs_to_walk = [SRC_DIR]

    for dir_path in dirs_to_walk:
        if not os.path.isdir(dir_path):
            continue
        for root, _dirs, files in os.walk(dir_path):
            for f in sorted(files):
                _, ext = os.path.splitext(f)
                if ext in SOURCE_EXTS:
                    sources.append(os.path.join(root, f))
    return sources


def check_staleness(src_path):
    """
    Returns (stale: bool, reason: str) for a source file.
    Stale if: index doesn't exist, or source is newer than index.
    """
    idx_path = src_to_index_path(src_path)
    if not os.path.exists(idx_path):
        return True, "missing"
    src_mtime = os.path.getmtime(src_path)
    idx_mtime = os.path.getmtime(idx_path)
    if src_mtime > idx_mtime:
        return True, "stale"
    return False, "fresh"


# ---------------------------------------------------------------------------
# Model integration
# ---------------------------------------------------------------------------

INDEX_TEMPLATE = """\
You are a senior software developer. Your job is to write a concise \
Semantic Index entry for a source file.

The index entry will be used by other developers (and AI agents) to quickly \
understand what the file does without reading the full source. It must follow \
this EXACT markdown format:

# SEMANTIC_INDEX/{relative_path}

## Summary
1-3 sentences: what this file does at a high level.

## When to Use
When another module or developer would reach for this file. Be specific.

## Public Types
* **TypeName:** `type_name` — one-line description.
(List key structs, enums, typedefs, classes, interfaces. Skip internal/private types.)

## Public Functions
* `FunctionName(args)`: One-line description of what it does.
(List the main API entry points. Skip internal/private helpers.)

RULES:
- Be concise. Each bullet should be ONE line.
- Use the exact section headers shown above.
- For header/interface files, focus on declarations. For implementation files, focus on behavior.
- If a section has no entries (e.g., a file with no public types), write "(None)" for that section.
- Include key constants, macros, or exported values if they are important for callers.
- Do NOT include implementation details or line-by-line commentary.
- Output ONLY the markdown index entry, nothing else.
"""

CHUNK_TEMPLATE = """\
You are a senior software developer. You are analyzing PART {chunk_num} \
of {total_chunks} of a large source file: {rel_path} ({total_lines} lines total).

This chunk covers lines {start_line}–{end_line}.

Extract from THIS CHUNK ONLY:
1) Any public types (structs, enums, classes, interfaces) with one-line descriptions.
2) Any public/important functions with one-line descriptions.
3) Any key constants, macros, or exports.
4) A 1-2 sentence summary of what this section of the file does.

Output a concise bullet-point list. Do NOT write a full index entry yet — \
this will be merged with other chunks later.
"""

MERGE_TEMPLATE = """\
You are a senior software developer. You previously analyzed a large \
source file in {total_chunks} chunks. Below are the partial results from each chunk.

Combine them into a SINGLE Semantic Index entry using this EXACT format:

# SEMANTIC_INDEX/{relative_path}

## Summary
1-3 sentences: what this file does at a high level.

## When to Use
When another module or developer would reach for this file. Be specific.

## Public Types
* **TypeName:** `type_name` — one-line description.

## Public Functions
* `FunctionName(args)`: One-line description of what it does.

RULES:
- Deduplicate entries that appear in multiple chunks.
- Be concise. Each bullet should be ONE line.
- For sections with no entries, write "(None)".
- Output ONLY the markdown index entry, nothing else.

--- CHUNK ANALYSES ---

{chunk_results}
"""


# Patterns that mark the start of a new semantic unit
_BOUNDARY_RE = re.compile(
    r"^("
    r"def\s|class\s|async\s+def\s"                  # Python
    r"|function\s|export\s|const\s+\w+\s*="          # JS/TS
    r"|func\s|type\s+\w+\s+struct"                   # Go
    r"|fn\s|pub\s|impl\s|struct\s|enum\s|trait\s"    # Rust
    r"|internal\s|void\s|static\s|inline\s"          # C/C++
    r"|typedef\s"                                     # C
    r"|@implementation\s|@interface\s"               # ObjC
    r"|public\s+class\s|private\s+class\s"           # Java/C#/Kotlin
    r"|// [-=]{3,}"                                  # section divider comments
    r"|# [-=]{3,}"                                   # section divider comments
    r"|#pragma\s+mark"                               # pragma mark sections
    r")"
)


def _is_boundary(line):
    """True if this line looks like the start of a new top-level definition."""
    return bool(_BOUNDARY_RE.match(line))


def number_lines(lines, offset=1):
    """Add line numbers to a list of lines."""
    numbered = []
    for i, line in enumerate(lines, offset):
        numbered.append(f"{i:>5} | {line.rstrip()}")
    return "\n".join(numbered)


def chunk_lines(lines, max_chars):
    """
    Split lines into chunks using semantic boundaries (function/struct/section
    starts). Falls back to a size cap when a single semantic block exceeds
    max_chars.
    """
    boundaries = [0]
    for i, line in enumerate(lines):
        if i > 0 and _is_boundary(line):
            boundaries.append(i)

    chunks = []
    current_lines = []
    current_chars = 0
    chunk_start = 0

    for seg_idx in range(len(boundaries)):
        seg_start = boundaries[seg_idx]
        seg_end = boundaries[seg_idx + 1] if seg_idx + 1 < len(boundaries) else len(lines)
        seg_lines = lines[seg_start:seg_end]
        seg_chars = sum(len(l) + 8 for l in seg_lines)

        if current_lines and current_chars + seg_chars > max_chars:
            chunks.append((chunk_start + 1, current_lines))
            current_lines = []
            current_chars = 0
            chunk_start = seg_start

        if seg_chars > max_chars and not current_lines:
            sub_lines = []
            sub_chars = 0
            for line in seg_lines:
                lc = len(line) + 8
                if sub_lines and sub_chars + lc > max_chars:
                    chunks.append((chunk_start + 1, sub_lines))
                    chunk_start += len(sub_lines)
                    sub_lines = []
                    sub_chars = 0
                sub_lines.append(line)
                sub_chars += lc
            current_lines = sub_lines
            current_chars = sub_chars
            chunk_start = seg_start + len(seg_lines) - len(sub_lines)
        else:
            current_lines.extend(seg_lines)
            current_chars += seg_chars

    if current_lines:
        chunks.append((chunk_start + 1, current_lines))

    return chunks


def build_prompt(src_path, source_content):
    """Build the prompt for indexing a file (single-shot)."""
    rel_path = index_relative_path(src_path)
    prompt = INDEX_TEMPLATE.format(relative_path=rel_path)
    prompt += f"\n\n=== SOURCE FILE: {rel_path} ===\n"
    prompt += source_content
    prompt += f"\n=== END: {rel_path} ===\n"
    prompt += f"\nNow write the Semantic Index entry for {rel_path}. Output ONLY the markdown."
    return prompt


def call_model(prompt):
    """Call the local Ollama API and return the response text."""
    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
    }).encode()

    req = urllib.request.Request(
        OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )

    try:
        resp = urllib.request.urlopen(req, timeout=TIMEOUT)
        data = json.loads(resp.read())
        return data.get("response", ""), None
    except urllib.error.URLError as e:
        return "", f"Could not reach Ollama at {OLLAMA_URL}: {e}"
    except Exception as e:
        return "", f"Model call failed: {e}"


def clean_response(response):
    """Strip any markdown fences or preamble the model might add."""
    text = response.strip()
    if text.startswith("```"):
        first_newline = text.index("\n") if "\n" in text else len(text)
        text = text[first_newline + 1:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip() + "\n"


# ---------------------------------------------------------------------------
# Core update logic
# ---------------------------------------------------------------------------

def update_one(src_path, dry_run=False):
    """Re-index a single source file. Returns (success, elapsed_seconds)."""
    rel = index_relative_path(src_path)
    idx_path = src_to_index_path(src_path)

    lines = []
    with open(src_path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    line_count = len(lines)
    total_chars = sum(len(l) for l in lines)

    needs_chunking = total_chars > CHUNK_TARGET_CHARS
    if needs_chunking:
        chunks = chunk_lines(lines, CHUNK_TARGET_CHARS)
        print(f"    {rel} ({line_count} lines, {total_chars:,} chars) -> {len(chunks)} chunks")
    else:
        print(f"    {rel} ({line_count} lines, {total_chars:,} chars)")

    if dry_run:
        return True, 0.0

    t0 = time.time()

    if not needs_chunking:
        source_content = number_lines(lines)
        prompt = build_prompt(src_path, source_content)
        response, error = call_model(prompt)
        if error:
            print(f"    ERROR: {error}")
            return False, time.time() - t0
        cleaned = clean_response(response)
    else:
        chunk_results = []
        for i, (start_line, chunk) in enumerate(chunks):
            end_line = start_line + len(chunk) - 1
            source_content = number_lines(chunk, offset=start_line)

            prompt = CHUNK_TEMPLATE.format(
                chunk_num=i + 1,
                total_chunks=len(chunks),
                rel_path=rel,
                total_lines=line_count,
                start_line=start_line,
                end_line=end_line,
            )
            prompt += f"\n=== {rel} (lines {start_line}-{end_line}) ===\n"
            prompt += source_content
            prompt += f"\n=== END CHUNK {i + 1} ===\n"

            print(f"      chunk {i+1}/{len(chunks)} (lines {start_line}-{end_line})...", end="", flush=True)
            response, error = call_model(prompt)
            if error:
                print(f" ERROR: {error}")
                return False, time.time() - t0

            chunk_results.append(f"### Chunk {i+1} (lines {start_line}-{end_line})\n{response.strip()}")
            print(f" done")

        print(f"      merging {len(chunks)} chunks...", end="", flush=True)
        merge_prompt = MERGE_TEMPLATE.format(
            total_chunks=len(chunks),
            relative_path=rel,
            chunk_results="\n\n".join(chunk_results),
        )
        response, error = call_model(merge_prompt)
        if error:
            print(f" ERROR: {error}")
            return False, time.time() - t0
        print(f" done")
        cleaned = clean_response(response)

    elapsed = time.time() - t0

    if not cleaned.startswith("#"):
        print(f"    WARNING: Response doesn't look like markdown, writing anyway")

    os.makedirs(os.path.dirname(idx_path), exist_ok=True)
    with open(idx_path, "w") as f:
        f.write(cleaned)

    print(f"    -> {os.path.relpath(idx_path, PROJECT_ROOT)} ({len(cleaned):,} chars, {elapsed:.1f}s)")
    return True, elapsed


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def fmt_duration(seconds):
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(seconds, 60)
    return f"{m:.0f}m {s:.0f}s"


def main():
    parser = argparse.ArgumentParser(
        description="Update stale Semantic Index entries using a local Ollama model."
    )
    parser.add_argument("--force", action="store_true",
                        help="Re-index even if the index entry is fresh")
    parser.add_argument("--all", action="store_true",
                        help="Process all files (by default, only stale ones)")
    parser.add_argument("--filter", metavar="PATTERN",
                        help="Only process files whose path contains PATTERN")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be updated without calling the model")
    parser.add_argument("files", nargs="*",
                        help="Specific source files to index (overrides scanning)")
    args = parser.parse_args()

    if not os.path.isdir(SRC_DIR):
        print(f"Error: Source directory not found: {SRC_DIR}")
        print(f"Edit SRC_DIR in update_index.py or move the script to your project root.")
        sys.exit(1)

    # Discover files
    if args.files:
        sources = [os.path.abspath(f) for f in args.files]
    else:
        sources = find_source_files()

    if args.filter:
        sources = [s for s in sources if args.filter.lower() in s.lower()]

    # Filter out skipped files
    def should_skip(path):
        rel = os.path.relpath(path, SRC_DIR)
        for pat in SKIP_PATTERNS:
            if rel.endswith(pat) or pat in rel:
                return True
        return False

    sources = [s for s in sources if not should_skip(s)]

    if not sources:
        print("No source files found.")
        print(f"  SRC_DIR: {SRC_DIR}")
        print(f"  SCAN_DIRS: {SCAN_DIRS or '(all subdirectories)'}")
        print(f"  SOURCE_EXTS: {sorted(SOURCE_EXTS)}")
        sys.exit(0)

    # Check staleness
    stale = []
    fresh = []

    for src in sources:
        is_stale, reason = check_staleness(src)
        if args.force or args.all or is_stale:
            stale.append((src, reason))
        else:
            fresh.append(src)

    # Report
    print(f"Semantic Index Update")
    print(f"{'─' * 50}")
    print(f"  Model:              {OLLAMA_MODEL}")
    print(f"  Source files found:  {len(sources)}")
    print(f"  Stale/missing:      {len(stale)}")
    print(f"  Fresh (skipped):    {len(fresh)}")
    print()

    if not stale:
        print("All index entries are up to date. Use --force to re-index.")
        sys.exit(0)

    if args.dry_run:
        print("[DRY RUN — no model calls will be made]\n")

    for src, reason in stale:
        rel = index_relative_path(src)
        tag = "FORCE" if (args.force and reason == "fresh") else reason.upper()
        print(f"  {tag:>8}  {rel}")

    print()

    # Process
    results = []
    wall_start = time.time()

    for i, (src, reason) in enumerate(stale):
        rel = index_relative_path(src)
        print(f"[{i+1}/{len(stale)}] {rel}")
        ok, elapsed = update_one(src, dry_run=args.dry_run)
        results.append((rel, ok, elapsed))
        print()

    total_elapsed = time.time() - wall_start

    # Summary
    print(f"{'═' * 50}")
    ok_count = sum(1 for _, ok, _ in results if ok)
    fail_count = len(results) - ok_count

    print(f"Done: {ok_count} updated, {fail_count} failed ({fmt_duration(total_elapsed)})")

    if fail_count:
        print("\nFailed files:")
        for rel, ok, _ in results:
            if not ok:
                print(f"  - {rel}")
        sys.exit(1)


if __name__ == "__main__":
    main()
