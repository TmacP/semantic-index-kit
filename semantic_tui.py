#!/usr/bin/env python3
"""
semantic_tui.py — Interactive TUI for updating the Semantic Index via a local Ollama model.

A curses-based interface that shows all your source files with their staleness
status (NEW / STALE / fresh). Pre-selects files that need updating, lets you
toggle selections, then batch-runs the model to regenerate index entries.

Usage:
    python3 semantic_tui.py
"""

import curses
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from update_index import (
    find_source_files, check_staleness, index_relative_path,
    update_one, fmt_duration, SRC_DIR, SKIP_PATTERNS, OLLAMA_MODEL,
)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_entries():
    """Discover all source files and compute staleness."""
    sources = find_source_files()

    def should_skip(path):
        rel = os.path.relpath(path, SRC_DIR)
        for pat in SKIP_PATTERNS:
            if rel.endswith(pat) or pat in rel:
                return True
        return False

    entries = []
    for src in sources:
        if should_skip(src):
            continue
        stale, reason = check_staleness(src)
        rel = index_relative_path(src)

        # Group by top-level directory
        parts = rel.split(os.sep)
        group = parts[0] if len(parts) > 1 else ""

        entries.append({
            "path": src,
            "rel": rel,
            "group": group,
            "stale": stale,
            "reason": reason,
            "selected": stale,  # pre-select stale/missing
        })
    return entries


# ---------------------------------------------------------------------------
# Curses TUI
# ---------------------------------------------------------------------------

HEADER_LINES = [
    "",
    "  Semantic Index — Ollama Updater",
    "  ───────────────────────────────",
    f"  Feeds source files to local {OLLAMA_MODEL}, writes SEMANTIC_INDEX/ entries.",
    "  Stale/missing files are pre-selected. Toggle with Space, Enter to run.",
    "",
]
HEADER_HEIGHT = len(HEADER_LINES)


def draw(stdscr, entries, cursor, force, scroll_offset):
    stdscr.erase()
    h, w = stdscr.getmaxyx()

    # Header
    for i, line in enumerate(HEADER_LINES):
        if i >= h - 6:
            break
        attr = curses.A_BOLD if i == 1 else (curses.A_DIM if i == 2 else curses.A_NORMAL)
        stdscr.addnstr(i, 0, line, w - 1, attr)

    # Entry list
    list_top = HEADER_HEIGHT
    list_bottom = h - 4
    visible = list_bottom - list_top

    prev_group = None
    draw_y = list_top

    for idx in range(len(entries)):
        e = entries[idx]

        if idx < scroll_offset:
            continue

        if draw_y >= list_bottom:
            break

        # Group separator
        if e["group"] != prev_group:
            if draw_y < list_bottom:
                label = f" ─── {e['group']}/ ───"
                stdscr.addnstr(draw_y, 0, label, w - 1, curses.A_DIM)
                draw_y += 1
            prev_group = e["group"]

        if draw_y >= list_bottom:
            break

        check = "●" if e["selected"] else "○"

        if e["reason"] == "missing":
            status = "NEW"
        elif e["stale"] or force:
            status = "FORCE" if (force and e["selected"]) else "STALE"
        else:
            status = "fresh"

        filename = os.path.basename(e["rel"])
        name_col = f" {check}  {filename}"

        max_name = w - 14
        if len(name_col) > max_name:
            name_col = name_col[:max_name - 1] + "…"

        attr = curses.A_REVERSE if idx == cursor else curses.A_NORMAL
        line = name_col.ljust(max_name) + status.rjust(7)
        stdscr.addnstr(draw_y, 0, line, w - 1, attr)
        draw_y += 1

    # Scroll indicators
    if scroll_offset > 0:
        stdscr.addstr(list_top, w - 2, "▲", curses.A_DIM)
    if draw_y >= list_bottom and scroll_offset + visible < len(entries):
        stdscr.addstr(list_bottom - 1, w - 2, "▼", curses.A_DIM)

    # Status bar
    bar_y = h - 3
    stdscr.addstr(bar_y, 0, "─" * min(w - 1, 60), curses.A_DIM)

    sel_count = sum(1 for e in entries if e["selected"])
    stale_count = sum(1 for e in entries if e["stale"])
    total = len(entries)
    tags = " [FORCE]" if force else ""

    info = f" {sel_count}/{total} selected ({stale_count} stale){tags}"
    stdscr.addnstr(bar_y + 1, 0, info, w - 1, curses.A_BOLD)

    # Help
    help_line = " ↑↓/jk Nav  Space Toggle  a All  n None  s Stale  f Force  Enter Run  q Quit"
    stdscr.addnstr(h - 1, 0, help_line, w - 1, curses.A_DIM)

    stdscr.refresh()


def tui_loop(stdscr):
    curses.curs_set(0)
    curses.use_default_colors()

    entries = load_entries()
    if not entries:
        stdscr.addstr(0, 0, "No source files found to index.")
        stdscr.addstr(1, 0, f"Check SRC_DIR in update_index.py (currently: {SRC_DIR})")
        stdscr.addstr(2, 0, "Press any key to exit.")
        stdscr.getch()
        return None

    cursor = 0
    force = False
    scroll_offset = 0

    while True:
        h, _ = stdscr.getmaxyx()
        visible = max(1, h - HEADER_HEIGHT - 4)

        cursor = max(0, min(cursor, len(entries) - 1))
        if cursor < scroll_offset:
            scroll_offset = cursor
        elif cursor >= scroll_offset + visible:
            scroll_offset = cursor - visible + 1

        draw(stdscr, entries, cursor, force, scroll_offset)
        key = stdscr.getch()

        if key == ord("q") or key == 27:
            return None

        elif key == curses.KEY_UP or key == ord("k"):
            cursor = max(0, cursor - 1)

        elif key == curses.KEY_DOWN or key == ord("j"):
            cursor = min(len(entries) - 1, cursor + 1)

        elif key == ord(" "):
            entries[cursor]["selected"] = not entries[cursor]["selected"]
            cursor = min(len(entries) - 1, cursor + 1)

        elif key == ord("a"):
            for e in entries:
                e["selected"] = True

        elif key == ord("n"):
            for e in entries:
                e["selected"] = False

        elif key == ord("s"):
            for e in entries:
                e["selected"] = e["stale"]

        elif key == ord("f"):
            force = not force

        elif key in (curses.KEY_ENTER, 10, 13):
            selected = [e for e in entries if e["selected"]]
            if not selected:
                continue
            return {
                "entries": selected,
                "force": force,
            }


# ---------------------------------------------------------------------------
# Runner (after curses exits)
# ---------------------------------------------------------------------------

def run_selected(selection):
    entries = selection["entries"]
    total = len(entries)

    print()
    print(f"{'═' * 60}")
    print(f"Updating {total} Semantic Index entries via {OLLAMA_MODEL}")
    print(f"{'═' * 60}")
    print()

    results = []
    wall_start = time.time()

    for i, e in enumerate(entries):
        print(f"[{i+1}/{total}] {e['rel']}")
        ok, elapsed = update_one(e["path"])
        results.append((e["rel"], ok, elapsed))
        print()

    total_elapsed = time.time() - wall_start

    # Summary
    print(f"{'═' * 60}")
    ok_count = sum(1 for _, ok, _ in results if ok)
    fail_count = total - ok_count

    for rel, ok, elapsed in results:
        icon = "✓" if ok else "✗"
        print(f"  {icon}  {rel}  ({fmt_duration(elapsed)})")

    print()
    if fail_count:
        print(f"{fail_count} failed, {ok_count} updated ({fmt_duration(total_elapsed)})")
    else:
        print(f"All {ok_count} entries updated ({fmt_duration(total_elapsed)})")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    selection = curses.wrapper(tui_loop)
    if selection is None:
        print("Cancelled.")
        sys.exit(0)

    run_selected(selection)
    sys.exit(0)


if __name__ == "__main__":
    main()
