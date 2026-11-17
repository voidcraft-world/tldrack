"""The session ledger: every agent conversation on this Mac, kept, and the human decisions in it.

── WHY IT EXISTS ───────────────────────────────────────────────────────────────
The rack's agents talk to the operator thousands of times a month, and every answer — a prompt, an
interrupt, an approved or denied tool, an AskUserQuestion pick, a plan sent back — is a labelled
example of "what did this agent need from a human, and what did the human want". That corpus is the
golden set for pinging the operator less: Laya's open-work verdict, the shadow decision judge, and
whatever comes after them all learn from it. Before this ledger it was being thrown away:

- **Claude Code deletes transcripts after 30 days** (`cleanupPeriodDays`, default 30). Every Claude
  account's `projects/` holds a month and no more; the bridge's own readers (`transcript.py`,
  laya-lab's turn-end export) read files that vanish.
- The bridge's side ledgers rotate or live in memory: the permission ledger keeps one `.1`, Laya's
  verdict ledger keeps two segments, the rack's state transitions and phone pushes were a 100-entry
  ring buffer.
- Codex keeps its rollouts, but nothing read them as data, and its guardian
  auto-review threads — a machine approver's verdict on every risky command — were invisible.

── WHAT IT KEEPS ───────────────────────────────────────────────────────────────
Two outputs in the `ledger` store (`store.py` owns the paths and the schema):

1. **The raw archive** (`raw/`): a lossless, compressed, append-only copy of every file under each
   Claude account's `projects/` and Codex's `sessions/`. A transcript grows by appended zstd frames,
   so a 200 MB conversation is never recompressed whole (`archive.py`). This is the backstop: any
   question nobody thought to index yet can still be answered from it.
2. **`ledger.sqlite`**: the index. One row per conversation file (main, subagent, workflow agent,
   headless call, Codex thread, Codex auto-review), its turns, its human and agent messages
   (full-text searchable), its tool calls with Claude's own permission provenance, and the EVENTS:
   the human decisions (`prompt`, `command`, `interrupt`, `permission`, `question`, `plan`) and the
   machine ones (`auto_review`, `auto_block`). The bridge's side ledgers land in `bridge_rows`.

── HOW IT RUNS ─────────────────────────────────────────────────────────────────
`tldrack ledger ingest` (`ingest.py`) is incremental and idempotent: a file whose size and
mtime did not move is skipped, a grown transcript appends its new lines to the archive and is
re-folded whole (cheap: the fold skips the multi-megabyte `api-request` lines unparsed), a shrunk or
rewritten one starts a new archive generation. The bridge runs it every `scheduler.INTERVAL_SECONDS`
in a CHILD process (`scheduler.py`), so a backfill never holds the GIL a hook is waiting on.

── WHAT NEVER HAPPENS ──────────────────────────────────────────────────────────
The folder is 0700 and its files 0600. The rack reads indexed text through the guarded local/tailnet
history routes (`reader.py`); no transcript is sent to AWS or a search service. Tool summaries are redacted
the permission ledger's way (`permission_ledger.summarize`); message text is kept as written, which
is the point — it is the operator's own text, on the operator's own disk, next to Claude's own copy.
"""
