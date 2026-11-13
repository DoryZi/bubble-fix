---
name: bubble-fix
description: Run a fix cycle on the issues the user dropped in the in-app bubble and the errors the app threw (one inbox per project), or heal errors in a new worktree. Use when the user says "fix the list", "fix the list fast", "run a fix cycle", "heal the errors", "/bubble-fix", "bubble-fix", "/fix-heal-loop", "fix-heal-loop", or when the bubble-fix collector starts a headless run. Works in Claude Code (/bubble-fix) and Codex ($bubble-fix).
---

# bubble-fix

The user reports issues in the app's bubble (text, pasted or picked images); the collector also catches
every error the back end logs and every error a page throws. They wait in the project's inbox
(`<project>/.bubble-fix/`). A **fix cycle** fixes the items the user picked, in their order. **Heal**
fixes errors only, in a new worktree by default, and waits for their OK to merge. Heal never starts
on its own: the user starts it (here, or Heal errors in the bubble).

The tool is `uv run --directory "${CLAUDE_PLUGIN_ROOT:-${BUBBLE_FIX_HOME:-$HOME/bubble-fix}}" python bubble_fix.py`. **Always type it out in full**, one command per call: no shell variables,
no `cd ... &&`, no pipes. Headless runs approve only this tool and the verify tool (typed in full),
`uv run pytest`, `uv run ruff`, `uv sync`, npm test scripts and plain `git <verb>`, so a shell variable for
the tool or `git -C <dir> status` is refused. Run git from the work checkout without `-C`.

`uv run --directory` changes the working folder, so always pass `--project "$PWD"` (or the project name).
If the project is not registered, run **Setup** first.

## Modes

| Call | What happens |
|---|---|
| `/bubble-fix` | Show the list, triage new items, ask Fix all / pick / plan-html, run the cycle. |
| `/bubble-fix fast [ids]` | "Fix the list fast": every open item (or the ids), in place, no triage, no questions. Clears the bubble's list. |
| `/bubble-fix heal [--here]` | Heal open errors (or the ids given). New worktree unless `--here`. |
| `/bubble-fix plan` | The full plan-html cycle on the list (or the ids): read the code, group the items, design new flows first, plan in rounds, then one fix cycle builds from the plan. |
| `/bubble-fix plan --project P --cycle N` | Headless, one round of a plan cycle started from the bubble's Plan in plan-html. |
| `/bubble-fix triage` | Label and size new items only. No code changes. |
| `/bubble-fix run --project P --cycle N` | Headless, started by the collector after Fix selected or Fix all. No questions. |
| `/bubble-fix heal --project P --cycle N` | Headless heal started from the bubble. No questions. |
| `/bubble-fix setup` | Register this project: app addresses, log folders, layout. |

## Rules (all modes)

- **The user's words are never reworded.** The tool copies their notes into the cycle intent exactly. Never
  edit the intent file, never paraphrase a note into an item title or a commit message as if they said it.
- **Never edit `.bubble-fix/` by hand.** Read and change the inbox only through the tool.
- **One cycle at a time per project.** The tool refuses a second one; say so and stop.
- **Never touch Live.** Fix in the project's work checkout (Development) or a worktree. No deploys, no pushes.
- **Never merge a heal branch without the user's OK.** Ask (interactive) or end with "branch ready, merge?" (headless).
- **Commits:** only after `verify commit` says OK TO COMMIT. Plain messages, no Co-Authored-By or Claude-Session lines.
- No em dashes or en dashes in anything you write for the user.

## 1. See the list and triage (interactive modes)

```bash
uv run --directory "${CLAUDE_PLUGIN_ROOT:-${BUBBLE_FIX_HOME:-$HOME/bubble-fix}}" python bubble_fix.py list --project "$PWD" --brief        # id, status, label, kind, count, title
uv run --directory "${CLAUDE_PLUGIN_ROOT:-${BUBBLE_FIX_HOME:-$HOME/bubble-fix}}" python bubble_fix.py show ID --project "$PWD"             # full item; file_paths are the images (Read them to see them)
```

For each item whose label is `new` (null) or has no proposal: look at it (Read every image path),
check it against the project's current intent, spec and mocks, and set:

```bash
uv run --directory "${CLAUDE_PLUGIN_ROOT:-${BUBBLE_FIX_HOME:-$HOME/bubble-fix}}" python bubble_fix.py triage ID --project "$PWD" --label broken|confusing|polish --size small|needs_call --proposal "one line: the fix you'd make"
```

- **broken**: an error, wrong data, something that doesn't work. **confusing**: the user can't tell what to do.
  **polish**: looks off.
- **needs_call**: the fix is a product decision (two reasonable ways, or it changes what a screen is for).
- Errors arrive as `broken` already; still add size and a proposal. Items the user dragged keep their order.

Show the user the list in their order: number, label, title, proposal, size. Short lines, no tables wider than the terminal.
If there are heal branches waiting (`git branch --list 'heal/*' 'fix/*'` in the work checkout, not merged), say so first.

## 2. Pick (interactive)

Ask once (Claude Code: AskUserQuestion; Codex: ask in chat):
- **Fix all N** (every open item), **Pick** (they name the numbers), or **Plan in plan-html** (go to section 5).
- Where: here (the work checkout) unless the tree has uncommitted changes, then offer a new worktree.
- Optional: a line from the user for this cycle (`--note`, copied into the intent as typed).

Items marked `needs_call` get their call before fixing; ask with the options you see.

## 3. Run the cycle

```bash
uv run --directory "${CLAUDE_PLUGIN_ROOT:-${BUBBLE_FIX_HOME:-$HOME/bubble-fix}}" python bubble_fix.py cycle-begin --project "$PWD" --ids 12 7 15 [--work worktree] [--note "their line, exactly"]
# headless: uv run --directory "${CLAUDE_PLUGIN_ROOT:-${BUBBLE_FIX_HOME:-$HOME/bubble-fix}}" python bubble_fix.py cycle-begin --project P --cycle N   (the collector already started it)
```

It prints JSON: `cycle.id`, `work_path`, `cycle_folder` (`cycles/YYYY-MM-DD-NN-fix-cycle-<id>/`, one folder
per cycle, in order like migrations), the `intent` and `spec` paths in it, and the items in order. Verify reports
go in that folder's `verify/`; private files (run log, review diff, record) in its `.work/`. Then:

1. **Write the spec** (`spec` path): replace every TODO with what fixed means for that item and how it
   is proven (a test, or a screenshot check at 1280 and 390 px). Do not touch the intent file.
   **A cycle with a `plan`** (cycle-begin prints it; it came from Plan in plan-html): the plan is the spec and the
   approval. Build its steps in order, check each intent's check, and verify with `--plan-md <plan>` and the intent.
   The spec already points to the plan; triage is not needed.
2. **Fix the items in order, in `work_path`.** Read the project's CLAUDE.md first and follow its rules
   (tests, file ownership, UI rules).
   - **Errors:** write a test that fails **with the same error as the traceback** (for example `KeyError: 7`).
     Run it and read the failure before you fix: an ImportError or NameError from a function that doesn't
     exist yet is not a reproduction. Then fix until it passes.
   - **UI items:** fix, then check the page at 1280 and 390 px (the project's e2e tests, or Playwright).
   - After each item: run the affected tests. Mark items fixed only after the commit (step 4): until then
     they are being fixed.
   - Two honest tries and still not fixed: `uv run --directory "${CLAUDE_PLUGIN_ROOT:-${BUBBLE_FIX_HOME:-$HOME/bubble-fix}}" python bubble_fix.py triage ID --project "$PWD" --label <same> --size needs_call --proposal "what you found"`
     and move on. It goes back on the list.
3. **Verify:** run the `verify` skill for this cycle (scope: the cycle's intent and spec files). On FIX FIRST,
   fix and verify again (at most two rounds), then stop and report.
4. **Commit** in `work_path`: run `verify commit` for real (the `/code-review low` skill, the checklist, the
   affected tests), then commit only on OK TO COMMIT. One commit for the cycle, message like
   `Fix cycle 4: stage chip on one line, import page date column` (item titles, no quotes of the user). Plain
   message: no Co-Authored-By or Claude-Session lines (the collector flags any it finds).
   If the cycle verify or `verify commit` could not run, **do not commit**: leave the changes in the work tree
   and start the summary with `Not committed:` and why. In a new worktree (`fix/<time>` or `heal/<time>`),
   end with the branch name and "merge?"; never merge it yourself without the user's yes.
5. **Mark and end:** after the commit, mark each fixed item:
   `uv run --directory "${CLAUDE_PLUGIN_ROOT:-${BUBBLE_FIX_HOME:-$HOME/bubble-fix}}" python bubble_fix.py mark ID ... --project "$PWD" --status fixed`.
   Not committed: mark nothing; the items go back on the list when the cycle ends.
   Then:
   ```bash
   uv run --directory "${CLAUDE_PLUGIN_ROOT:-${BUBBLE_FIX_HOME:-$HOME/bubble-fix}}" python bubble_fix.py cycle-end CYCLE --project "$PWD" --summary "Fixed 3 of 4. Verify SHIP (report path). Commit abc123. Needs you: item 15 (why)."
   ```
   Unfixed items go back on the list; next-cycle items come back on top. The bubble shows the first line of the summary.

Tell the user (interactive): fixed, not fixed and why, verify verdict and report path, commit, what needs them.

## 3b. Fast (`fast`, "fix the list fast")

The user uses the bubble only to capture issues and clears them from the harness. No triage, no questions, no plan deck.

1. `git status --porcelain` in the work checkout. Uncommitted changes that are not yours: stop and name the files
   (never mix them into the fix commit).
2. Pick the items: the ids the user gave, else every open item from `uv run --directory "${CLAUDE_PLUGIN_ROOT:-${BUBBLE_FIX_HOME:-$HOME/bubble-fix}}" python bubble_fix.py list --project "$PWD" --brief` except those
   marked Needs your call (size needs_call): they wait for their call and are taken only when they name them. Then
   `uv run --directory "${CLAUDE_PLUGIN_ROOT:-${BUBBLE_FIX_HOME:-$HOME/bubble-fix}}" python bubble_fix.py cycle-begin --project "$PWD" --ids 12 7 15 [--note "their line, exactly"]` (work here).
   The bubble shows the items in its Fixing card within a second.
3. Write the spec (one line per item: what fixed means), then fix in order as in section 3 step 2. An item that is a
   product decision, or still not fixed after two honest tries:
   `uv run --directory "${CLAUDE_PLUGIN_ROOT:-${BUBBLE_FIX_HOME:-$HOME/bubble-fix}}" python bubble_fix.py triage ID --project "$PWD" --label <same> --size needs_call --proposal "why"` and move on (it goes back on
   the list marked Needs your call). Do not ask.
4. Affected tests after each item, then `verify commit` for real and one commit on OK TO COMMIT (section 3 step 4).
   The full cycle verify is skipped in fast mode; say so in the summary ("Cycle verify skipped (fast)").
5. Mark each fixed item: `uv run --directory "${CLAUDE_PLUGIN_ROOT:-${BUBBLE_FIX_HOME:-$HOME/bubble-fix}}" python bubble_fix.py mark ID ... --project "$PWD" --status fixed` (never delete items: fixed ones leave
   the list, and an error that comes back reopens). Then
   `uv run --directory "${CLAUDE_PLUGIN_ROOT:-${BUBBLE_FIX_HOME:-$HOME/bubble-fix}}" python bubble_fix.py cycle-end CYCLE --project "$PWD" --summary "Fixed 3 of 4. Cycle verify skipped (fast). Commit abc123. Needs you: item 15 (why)."`
   Tell the user: fixed, needs their call and why, commit.

## 4. Heal

Same as section 3 with these changes:
- Items: open errors only (`--mode heal`; no ids = the first 3 open errors). At most 3 per run; the rest
  wait for the next run. Notes are refused.
- Work: a new worktree by default (`--work worktree`); `--here` means the work checkout.
- Each error: reproduce it with a failing test first, then fix. One commit per error after `verify commit`.
- End with the branch name and "merge?". Interactive: ask the user; merge only on their yes
  (`git -C <work checkout> merge --no-ff heal/<time>`), then `git worktree remove` the heal worktree.
  Headless: put "Branch heal/<time> ready. Merge?" first in the summary.

## 5. Plan in plan-html (the full plan-html cycle)

Plan in plan-html is the whole plan-html cycle over the items, not the list of bugs in a deck: read the code, group
the items into intents, design new or changed flows first, plan in rounds, then one fix cycle builds from the plan.
(Fix selected is the short path: the user's words as the intent, a short spec, the fix.)

**One round per run** (headless, started from the bubble: `/bubble-fix plan --project P --cycle N`):

1. `uv run --directory "${CLAUDE_PLUGIN_ROOT:-${BUBBLE_FIX_HOME:-$HOME/bubble-fix}}" python bubble_fix.py plan-begin --project P --cycle N` prints `round`; `intent` (the user's words, exactly, in the cycle folder);
   `plan` (the deck's plan.json to write, in the cycle's `.work/deck/`); `answers` (from round 2: the file with the user's
   answers to the last round); `plan_html` (the skill folder); `work_path`; and the items, each with `images` (Read
   every one) and `card_images` (the same images as a card shows them: `![](files/x.png)`).
2. Load the plan-html skill (Claude Code: `/plan-html`; Codex: read `<plan_html>/SKILL.md`) and follow it with the
   intent as the request. There is nobody to ask in chat, so:
   - **Round 1:** Stage 0 (earlier cycles), then read the code every item touches (Stage 2; Claude: explore agents,
     Codex: yourself). Group the items into intents in the user's words and put them in an order that makes sense (what
     blocks what, what shares a screen). **Every item id goes in exactly one card's `items`** (`"items": [12, 7]`): an
     intent (it gets fixed) or a boundary (left for later, say why).
   - **One round with everything in it:** when an item needs a new or changed flow (what the user clicks, what opens, a
     screen that changes shape), that item's question shows 2 or 3 options as strips of mock screens (Stage 3a: files
     under `files/mocks/` next to plan.json; run check_prototype.py until MOCKS OK), and the intents and steps are
     written for the option you recommend in the same deck (Stage 3b). No separate design round. A question you
     would ask in chat becomes a `needs-you` card.
   - **From round 2:** read `answers` and do plan-html's round handling (answers, notes; on Lock the design, write the
     plan from the prototype). Set `round` to the round plan-begin printed and bump `rev`.
   - Write plan.json at `plan`. **Do not serve it:** the collector serves it when you end.
3. `uv run --directory "${CLAUDE_PLUGIN_ROOT:-${BUBBLE_FIX_HOME:-$HOME/bubble-fix}}" python bubble_fix.py plan-check --project P --cycle N` must print `"ok": true`; fix what it names. Change no project code. End with
   one line: what ttheir round asks the user.

The collector serves the deck (the link shows in the bubble), saves the user's round and starts the next run. On
Finalize it writes plan.md and plan.json into the cycle folder (plan-html's finalize_plan.py), commits them with
the prototype, and queues one fix cycle with the items of the intents they kept. Struck intents and boundaries leave
their items on the list. Stop planning in the bubble ends the cycle.

**In a session** (`/bubble-fix plan`, Claude Code or Codex): the user is here, so run plan-html as usual (ask in chat,
serve the deck yourself as the plan-html skill says), with the deck at `plan`:

```bash
uv run --directory "${CLAUDE_PLUGIN_ROOT:-${BUBBLE_FIX_HOME:-$HOME/bubble-fix}}" python bubble_fix.py plan-begin --project "$PWD" [--ids 12 7]                 # a new plan cycle; prints the same as above
uv run --directory "${CLAUDE_PLUGIN_ROOT:-${BUBBLE_FIX_HOME:-$HOME/bubble-fix}}" python bubble_fix.py plan-end --project "$PWD" --cycle N --round <file>       # after Finalize: plan.md + plan.json, committed
```

`plan-end` prints the items to fix and the plan: run section 3 with
`uv run --directory "${CLAUDE_PLUGIN_ROOT:-${BUBBLE_FIX_HOME:-$HOME/bubble-fix}}" python bubble_fix.py cycle-begin --project "$PWD" --ids <ids> --plan <plan>`.

## 6. Headless runs (started by the collector)

The prompt is `/bubble-fix run --project P --cycle N` (or `heal ...`, or `plan ...`: one plan round, section 5).
A project registered with `--harness codex` runs them in Codex (`$bubble-fix ...`, sandboxed). There is nobody to ask:
- **Step 1, always: triage.** For every item of this cycle with no label or no proposal, run section 1's
  `triage` command (look at every image with Read first). The bubble shows these labels and proposals.
  Then do section 3 (or 4). Skip `needs_call` items: leave them open with their proposal.
- The collector already wrote the intent and spec skeleton in the cycle folder; `cycle-begin --cycle N` prints
  its paths (`cycle_folder`, `intent`, `spec`).
- Only a few Bash commands are approved: this tool and the verify tool (typed in full), `uv run pytest`,
  `uv run ruff`, `uv sync`, npm test scripts and plain `git <verb>` from the work checkout.
- The cycle verify still runs in a fresh agent: spawn the verifier and put the command rule above at the top
  of its brief. Checking your own work instead does not count.
- **`verify commit` in a headless run:** do not call the built-in `/code-review` here. It builds shell pipes this
  run can't approve, and its empty "(none)" result then looks clean when nothing was reviewed. Instead:
  1. Run `uv run --directory "${CLAUDE_PLUGIN_ROOT:-${BUBBLE_FIX_HOME:-$HOME/bubble-fix}}" python bubble_fix.py review-diff --project P CYCLE`.
     It stages everything and writes the **whole** staged diff to a file; it prints the path.
  2. Spawn a fresh review agent. Put the command rule at the top of its brief and give it **the path only**:
     it reads the file itself with Read, so nothing is trimmed or summarized by you. Ask for correctness
     bugs, secrets, out-of-scope files, weakened tests and leftovers, and to end with exactly
     `REVIEW: NO FINDINGS` or a numbered list of findings.
  3. Fix any findings, run the cycle verify again (a code change after verify needs a new verify), and
     review once more. Run the affected tests.
  4. Commit only when the last review ended with `REVIEW: NO FINDINGS` and the cycle verify said SHIP.
     Anything else, including an empty or unclear review: `Not committed:` first in the summary, changes left staged.
- Never merge. End with `cycle-end` and a summary whose first line says what happened.

## 7. Setup (once per project)

Ask the user for the app addresses (for example Development `http://localhost:8776` and Live
`http://localhost:8766`) and where the app writes logs. Then:

```bash
uv run --directory "${CLAUDE_PLUGIN_ROOT:-${BUBBLE_FIX_HOME:-$HOME/bubble-fix}}" python bubble_fix.py register --setup --name NAME --path "$PWD" --origin http://localhost:8776 --origin http://localhost:8766 \
  --log 'state/logs/*.jsonl:jsonl' --log 'logs/server.log:text' [--work-dir <Development checkout>]
```

`--setup` gitignores `.bubble-fix/` and adds the rules block to CLAUDE.md and AGENTS.md (`--no-docs`
to skip). Add the bubble to the app's pages with one line, only where the collector runs:

```html
<script src="http://127.0.0.1:8797/bubble.js" defer></script>
```

Restyle it with CSS custom properties on `:root` if the app's own tokens are not picked up:
`--bubble-fix-ink --bubble-fix-muted --bubble-fix-bg --bubble-fix-soft --bubble-fix-line --bubble-fix-accent --bubble-fix-on-accent --bubble-fix-radius --bubble-fix-font`.

## Running in Codex

`$bubble-fix`. No AskUserQuestion: ask in chat. For `verify`, follow the verify skill's Codex notes.
Plan in plan-html in a session: serve without `--live`, in the foreground, as the plan-html skill says.
Headless Codex runs (a project registered with `--harness codex`) are sandboxed and have no agents: where this skill
says to spawn a fresh agent (the verifier, the review of `review-diff`), do that check yourself in a separate pass,
re-reading the docs and the diff file from disk, and say so in the summary.
