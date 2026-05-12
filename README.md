# Semantic Index Kit

A local-LLM-powered semantic index for any codebase. Scans your source files, detects which index entries are stale or missing, and regenerates them using a local [Ollama](https://ollama.com) model (Gemma, Llama, Qwen, etc.).

Comes with an interactive **curses TUI** that lets you see staleness at a glance and pick which files to re-index before burning inference tokens.

```
  Semantic Index — Ollama Updater
  ───────────────────────────────
  Feeds source files to local gemma3:12b, writes SEMANTIC_INDEX/ entries.
  Stale/missing files are pre-selected. Toggle with Space, Enter to run.

 ─── api/ ───
 ●  routes.py                                      STALE
 ●  auth.py                                          NEW
 ○  models.py                                      fresh
 ─── core/ ───
 ●  engine.py                                      STALE
 ○  config.py                                      fresh
 ──────────────────────────────────────────────────────
 3/5 selected (3 stale)
 ↑↓/jk Nav  Space Toggle  a All  n None  s Stale  f Force  Enter Run  q Quit
```

## How It Works

1. **Staleness = file modification time.** If `src/foo.py` is newer than `SEMANTIC_INDEX/foo_py.md`, it's stale. No database, no manifest.
2. **Chunked analysis for large files.** Files over 40K chars are split at semantic boundaries (function/class/struct definitions), analyzed in parts, then merged into one index entry.
3. **Index entries follow a fixed format** that AI agents and developers can both parse quickly:

```markdown
# SEMANTIC_INDEX/core/engine.py

## Summary
Core game loop and state management...

## When to Use
When modifying the main update cycle or adding new subsystems.

## Public Types
* **EngineState:** `engine_state` — Top-level state container.

## Public Functions
* `run(config)`: Start the main loop with the given config.
```

## Prerequisites

- **Python 3.7+** (no external dependencies — uses only stdlib)
- **[Ollama](https://ollama.com)** running locally with a model pulled:
  ```bash
  # Install Ollama, then pull a model:
  ollama pull gemma3:12b     # recommended default
  # or: ollama pull llama3.2, qwen2.5, etc.
  ```

## Setup

### 1. Clone into your project

```bash
cd your-project
git clone https://github.com/TmacP/semantic-index-kit.git tools/semantic-index
```

Or copy the two Python files anywhere in your repo.

### 2. Configure paths

Edit the **Config** section at the top of `update_index.py`:

```python
# Root of your project
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "."))

# Where your source code lives
SRC_DIR = os.path.join(PROJECT_ROOT, "src")

# Where index entries are written
INDEX_DIR = os.path.join(PROJECT_ROOT, "SEMANTIC_INDEX")

# Subdirectories to scan (empty = scan all of SRC_DIR)
SCAN_DIRS = []  # e.g., ["api", "core", "utils"]

# Files/patterns to skip
SKIP_PATTERNS = []  # e.g., ["vendor/", "node_modules/", "generated/"]
```

### 3. Choose your model

Default is `gemma3:12b`. Override via environment variable:

```bash
export OLLAMA_MODEL=llama3.2
# or pass inline:
OLLAMA_MODEL=qwen2.5:14b python3 tools/semantic-index/semantic_tui.py
```

### 4. (Optional) Add a Makefile target

```makefile
semantic:
	@python3 tools/semantic-index/semantic_tui.py
```

## Usage

### Interactive TUI (recommended)

```bash
python3 semantic_tui.py
# or: make semantic
```

| Key | Action |
|-----|--------|
| `↑↓` / `j k` | Navigate |
| `Space` | Toggle selection |
| `a` | Select all |
| `n` | Select none |
| `s` | Select stale only (reset) |
| `f` | Toggle force mode (re-index fresh files too) |
| `Enter` | Run selected |
| `q` / `Esc` | Quit |

### CLI (scriptable)

```bash
# Update only stale files
python3 update_index.py

# Preview what would change
python3 update_index.py --dry-run

# Force re-index everything
python3 update_index.py --force --all

# Only files matching a pattern
python3 update_index.py --filter auth

# Specific files
python3 update_index.py src/api/routes.py src/core/engine.py
```

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OLLAMA_URL` | `http://localhost:11434/api/generate` | Ollama API endpoint |
| `OLLAMA_MODEL` | `gemma3:12b` | Model name |
| `INDEX_TIMEOUT` | `180` | Timeout per model call (seconds) |

## Using with Claude Code (or other AI agents)

The whole point of a semantic index is to give AI agents fast orientation in your codebase. Add this snippet to your project's `CLAUDE.md` (or equivalent agent instructions):

```markdown
## Semantic Index — Always Read, Always Update

`SEMANTIC_INDEX/` mirrors `src/` with Markdown summaries of each source file.

### Before you touch code — read the index first
When starting any task, read the relevant `SEMANTIC_INDEX/` files for the
modules you'll be working in. This orients you on types, APIs, and module
boundaries before you dive into source.

### After you finish work — update the index
At the end of every response where you created or modified source files,
update the corresponding `SEMANTIC_INDEX/` entries to reflect your changes.

### Index file format
Each file should include:
- **Summary:** 1-3 sentence high-level description.
- **When to Use:** When another module or developer would reach for this file.
- **Public Types:** Key structs/enums/classes with one-line descriptions.
- **Public Functions:** API entry points with one-line descriptions.
```

## Supported Languages

Out of the box, the scanner recognizes these extensions:

`.py` `.js` `.ts` `.tsx` `.jsx` `.cpp` `.c` `.h` `.hpp` `.cc` `.go` `.rs` `.java` `.kt` `.swift` `.rb` `.lua` `.zig` `.odin` `.cs` `.mm` `.metal` `.wgsl` `.glsl`

Edit `SOURCE_EXTS` in `update_index.py` to add or remove languages.

## License

MIT
