# Copilot Instructions (Aligned to Authoritative Working Contract v3.1)

## Authority & Scope
- Treat the currently provided/selected/pasted files (or explicitly named files) as authoritative.
- Do not assume or reference code outside the visible scope unless the user explicitly provides it.
- If a fix seems to belong in another file, STOP and propose: which file(s), why, and what minimal change. Do not implement without approval.

## Unknowns Handling (Hard Rule)
- If required classes/attributes/methods/invariants are not visible, DO NOT guess, invent, or “work around” them.
- Do not add defensive guards (`getattr`, `hasattr`, fallback names, try/except to suppress).
- Instead, list the missing definitions explicitly and ask for the needed file(s) or confirmation.

## Coding Style & Change Discipline
- No defensive coding unless explicitly requested.
- No incidental refactors: no formatting-only edits, import reordering, renames, logging, comments, typing churn, or unrelated cleanup.
- Keep changes minimal, intentional, and semantically precise.
- Preserve existing semantics, naming, journaling/audit contracts, and deterministic ordering patterns present in the visible code.
- **Never add a `try/except` block without a traceback.** Any new `except` clause that catches `Exception` (or a broad base type) in a non-trivial code path must call `report_exception(...)` (from `trading_platform.utils.diag_logger`) or, if that import is unavailable in the current file context, at minimum `traceback.print_exc()`. A bare `except Exception: pass` or `except Exception: return` is never acceptable for new code. If the block is genuinely cosmetic/safe (e.g. optional Qt import fallback, painter cleanup), add a one-line comment explaining why swallowing is intentional.

## Plan Mode — Save Plan & Chat Snippets
When a plan is produced in plan mode (or any response that is primarily a structured
implementation plan), first assess whether the plan requires **multiple chats** to implement
(i.e. it touches many files, has sequential phases, or would exhaust a single context window).

**Context window note (as of 2026-06):** Claude supports 200k tokens per context and GPT supports 275k tokens per context. Prefer a single chat unless the plan has 3+ sequential phases that each depend on prior output, touches 10+ files, or would exceed ~150k tokens for Claude or ~200k tokens for GPT of combined prompt + implementation. Do not split merely because a plan is large — exhaust the single-chat option first.

**If the plan can be fully implemented in a single chat** (simple scope, few files), only
suggest saving the plan file — do NOT suggest a chat snippets file:

> **Plan ready.** Consider saving this to disk:
> - **Plan file:** `docs/TODO/YYYY-MM-DD_<Descriptive_Topic_Name>_Plan_v1_0.md`
>
> File naming rules:
> - Prefix with today's date in `YYYY-MM-DD` format
> - Use a concise, descriptive topic name (e.g. `Bounce_Detection_Engine`)
> - Append `_Plan_v1_0`; use underscores, no spaces
>
> **Plan file structure:** The plan file must open with a short high-level synopsis (2–4 sentences) summarising the problem, the approach, and the expected outcome — before any phase/step breakdown.
>
> Do not write the file automatically — only prompt the user to do so.

**If the plan requires multiple chats**, suggest both the plan file and a chat snippets file:

> **Plan ready.** Consider saving this to disk:
> - **Plan file:** `docs/TODO/YYYY-MM-DD_<Descriptive_Topic_Name>_Plan_v1_0.md`
> - **Chat snippets file:** `docs/TODO/YYYY-MM-DD_<Descriptive_Topic_Name>_Chat_Snippets.md`
>
> File naming rules:
> - Prefix with today's date in `YYYY-MM-DD` format
> - Use a concise, descriptive topic name derived from the plan content (e.g.
>   `Bounce_Detection_Engine`, `Order_Risk_Gate`, `Bar_Finalization_Refactor`)
> - Plan file: append `_Plan_v1_0`; Chat snippets file: append `_Chat_Snippets`
> - Use underscores, no spaces
>
> **Plan file structure:** The plan file must open with a short high-level synopsis (2–4 sentences) summarising the problem, the approach, and the expected outcome — before any phase/step breakdown.
>
> The snippets file should contain the **minimum number of sequentially ordered chat
> prompts** required to implement the plan without exhausting the context window of the
> implementing model. Each snippet must be self-contained (include all necessary file
> paths, symbols, and intent) so it can be pasted directly into a fresh chat.
>
> **Model continuity:** The snippets file must open with a one-line note identifying
> which model produced the plan so the same model implements it:
> - Claude Sonnet → `<!-- Planned by: Claude Sonnet — implement with Claude Sonnet -->`
> - ChatGPT (GPT-4o / o-series) → `<!-- Planned by: ChatGPT — implement with ChatGPT -->`
> - Gemini → `<!-- Planned by: Gemini — implement with Gemini -->`
> - Grok → `<!-- Planned by: Grok — implement with Grok -->`
>
> Do not write the files automatically — only prompt the user to do so.

## Chat Snippets Completion Prompt
After implementing **each snippet** (every chat/phase, not just the last), end the response with a prompt like:

> **Snippet N complete.** Paste the next snippet when ready, or open a fresh chat with Snippet N+1.

Identify the snippet number and files from the attachment or context provided at the start of the chat.
On the **final snippet** (no further snippets remain), replace the above prompt with a devlog prompt:

> **Plan complete.** Update `docs/devlog/current.md` before committing:
> - **Date / title** — today's date and a one-line summary of the plan topic
> - **Problem** — what was broken or missing
> - **Cause** — root cause identified
> - **Dead ends** — any `Fix failed:` declarations from this chat (or "none")
> - **Solution** — what was changed and why
> - **Files touched** — list of modified files
> - **Verification** — how to confirm it works
> - **Follow-ups** — any known gaps or next steps
>
> Also append one line to `docs/devlog/fix_index.md`: `YYYY-MM-DD | <title> | <component or key file>`
>
> Say **"update devlog"** to have both files written at once.
>
> Then: `git add docs/devlog/current.md docs/devlog/fix_index.md` and commit everything together.

When the user says **"update devlog"**, write both files:
1. Append the entry to `docs/devlog/current.md`
2. Append one line to `docs/devlog/fix_index.md`: `YYYY-MM-DD | <title> | <component or key file>`

Do not write the devlog entry automatically — only prompt the user to do so.

## Fix Failed Declaration
When the user says something that sounds like a fix was tried but did not work — phrases such as "that didn't work", "still broken", "same issue", "no change", "reverted", "back to square one", "still failing", "no luck" — respond with a clarifying prompt before continuing:

> Did that fix fail? If so, confirm with: `Fix failed: <what was tried> — <why it failed>`
> I'll include it in the devlog under **Dead ends** so it's never re-proposed.

If the user confirms with `Fix failed: ...`, record the dead end and include it in the devlog prompt at end-of-chat.

## Fix Success Declaration
When the user writes "Fix success" (or a phrase like "that worked", "fixed it", "it's working now"), infer from the conversation context what was fixed and respond with a confirmation summary:

> **Fix success recorded:** `Fix success: <what was fixed> — <brief reason it worked>`
> I'll include it in the devlog under **Solution** at end-of-chat.

If context is ambiguous, ask the user to confirm: `Fix success: <inferred fix> — is this correct?`

Record the confirmed success and include it in the devlog prompt at end-of-chat under **Solution**.

## Development Log
At the end of any chat that modifies files and is **not** covered by the Chat Snippets Completion Prompt above, prompt the user to fill in `docs/devlog/current.md` before committing:

> **Devlog ready to fill?** Update `docs/devlog/current.md` before committing:
> - **Date / title** — today's date and a one-line summary
> - **Problem** — what was broken or missing
> - **Cause** — root cause identified
> - **Dead ends** — any `Fix failed:` declarations from this chat (or "none")
> - **Solution** — what was changed and why
> - **Files touched** — list of modified files
> - **Verification** — how to confirm it works
> - **Follow-ups** — any known gaps or next steps
>
> Also append one line to `docs/devlog/fix_index.md`: `YYYY-MM-DD | <title> | <component or key file>`
>
> Say **"update devlog"** to have both files written at once.

Do not write the devlog entry automatically — only prompt the user to do so.

## Devlog Archive & Index
`docs/devlog/current.md` is a rolling window. When it grows large (roughly quarterly), rotate older entries into `docs/devlog/archive/YYYY-QN.md` and clear `current.md` back to the blank template.

`docs/devlog/fix_index.md` is a flat one-liner lookup — one entry per completed devlog. When a user asks "was this fixed before?" or "have we seen this bug?", read `fix_index.md` first before attaching full devlog history. Do not attach `current.md` or archive files unless the index suggests a match.

## General Principles
- Always ask for clarification when uncertain about intent, scope, or details.