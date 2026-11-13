#!/usr/bin/env bash
#
# sync-from-source.sh: regenerate this OSS repo from the working copy.
#
# The source of truth is agent_tools/bubble_fix in the main repo. This repo is
# the published copy: do not edit the synced files here, change the source and
# run this again. It is safe to run any number of times.
#
# Sync copies the tool (bubble_fix.py, lib/, bubble/, skill/, rules.md, the
# service template, pyproject.toml, uv.lock, tests/) and the public-only files
# from the source's publish/templates/ (README, LICENSE, CHANGELOG, .gitignore,
# .claude-plugin/). It never copies state/, FIXLOG.md, caches, virtualenvs or
# any inbox database. Then the source's publish/portablize.py (not published)
# makes the copy portable: machine-local commands take their root from
# CLAUDE_PLUGIN_ROOT or BUBBLE_FIX_HOME at run time, the owner's name and
# source-repo notes are removed, and it fails loudly if anything machine-local
# or personal is left.
#
# After syncing run: uv run pytest -q -m "not e2e"
#
# Usage:
#   ./scripts/sync-from-source.sh [--dry-run]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SOURCE_REPO="$(cd "$REPO_DIR/../.." && pwd)"
SRC="$SOURCE_REPO/agent_tools/bubble_fix"
TEMPLATES="$SRC/publish/templates"

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

[[ -d "$SRC" ]] || { echo "missing source: $SRC" >&2; exit 1; }

copy() {
    local src="$1" dst="$2"
    if [[ ! -f "$src" ]]; then echo "missing source: $src" >&2; exit 1; fi
    if $DRY_RUN; then echo "would copy: $src -> $dst"; else
        mkdir -p "$(dirname "$dst")"; cp "$src" "$dst"; echo "copied: $dst"
    fi
}

# Folders are replaced whole, so a file removed in the source goes from here too.
copy_dir() {
    local src="$1" dst="$2"
    if [[ ! -d "$src" ]]; then echo "missing source: $src" >&2; exit 1; fi
    if $DRY_RUN; then echo "would copy: $src -> $dst"; else
        rm -rf "$dst"; mkdir -p "$dst"
        (cd "$src" && find . -type f -not -path '*/__pycache__/*' -not -name '*.pyc' -not -path '*/.pytest_cache/*' \
            -not -path '*/.venv/*' -not -path '*/.ruff_cache/*' -print0 | while IFS= read -r -d '' f; do
            mkdir -p "$dst/$(dirname "$f")"; cp "$f" "$dst/$f"
        done)
        echo "copied: $dst"
    fi
}

for f in bubble_fix.py pyproject.toml uv.lock rules.md bubble-fix.service skill/SKILL.md; do
    copy "$SRC/$f" "$REPO_DIR/$f"
done
copy_dir "$SRC/lib" "$REPO_DIR/lib"
copy_dir "$SRC/bubble" "$REPO_DIR/bubble"
copy_dir "$SRC/tests" "$REPO_DIR/tests"

# Plugin-marketplace discovery reads skills/bubble-fix/SKILL.md.
copy "$SRC/skill/SKILL.md" "$REPO_DIR/skills/bubble-fix/SKILL.md"

# Public-only files.
for f in README.md LICENSE CHANGELOG.md .gitignore .claude-plugin/plugin.json .claude-plugin/marketplace.json; do
    copy "$TEMPLATES/$f" "$REPO_DIR/$f"
done

if $DRY_RUN; then echo "would portablize the copy"; else
    python3 "$SRC/publish/portablize.py" "$REPO_DIR"
fi

echo
echo "Run 'uv run pytest -q -m \"not e2e\"' to verify the copy."
