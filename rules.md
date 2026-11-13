## Bubble-fix

Issues the user drops in the in-app bubble and the errors the app throws collect in `.bubble-fix/` (gitignored).

- **See the list and run a fix cycle:** the `bubble-fix` skill (`/bubble-fix` in Claude Code, `$bubble-fix` in Codex), or Fix selected in the bubble.
- **Heal errors:** `/bubble-fix heal` (a new worktree, merged only after the user's OK). Heal never starts on its own.
- Each fix cycle gets its own folder, in order like migrations: `cycles/YYYY-MM-DD-NN-fix-cycle-<id>/` with its intent (the user's own notes, copied exactly), spec and verify reports. Its run log, review diff and record are in that folder's `.work/` (gitignored).
- Never edit `.bubble-fix/` files by hand: use `uv run --directory "${CLAUDE_PLUGIN_ROOT:-${BUBBLE_FIX_HOME:-$HOME/bubble-fix}}" python bubble_fix.py`.
