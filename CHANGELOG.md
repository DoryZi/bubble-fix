# Changelog

All notable changes to **bubble-fix** are documented here.

## 0.1.0

First public version.

- **In-app bubble.** A floating bubble you add to your app with one script line. Drop an issue
  as text, pasted or picked images; the page's own errors are caught too.
- **Error inbox per project.** A local collector keeps one inbox per project (`.bubble-fix/`),
  reads the errors your back end logs (JSON lines or plain text), scrubs emails and phone
  numbers from error text, and groups repeats.
- **Fix cycles.** Pick items in your order, press Fix selected (or run `/bubble-fix`): your
  notes are copied exactly into the cycle's intent, a spec is written, the fix is made, and a
  fresh verify and review run before anything is committed.
- **Heal cycles.** Fix errors in a new worktree; the branch waits for your OK to merge.
- **Plan in plan-html.** Optional: plan the items in rounds in a plan-html deck, then build.
- **Claude Code or Codex.** Each project runs its headless cycles with `claude -p` (default) or
  `codex exec`.
- **Collector service.** A systemd user service on 127.0.0.1, installed by `install`.
