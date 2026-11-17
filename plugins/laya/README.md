# laya — Laya decisions on this machine

A tldrack plugin that runs [Laya](https://huggingface.co/convaiinnovations/laya)
(`convaiinnovations/laya`, Apache-2.0) locally and answers `/v1/systemone` decisions — typed
`noul` / `choice` / `score` questions with probabilities — over the bridge's loopback port. It also
keeps the decision ledger and serves the REVIEW deck and AGENT RACK › LAYA.

Laya is a ~420M encoder (ModernBERT-large plus a 2-layer decision head), not an LLM: it never
writes text, it scores answers. A warm decision is sub-second; `laya-lab latency` reads the real
figure off this machine's own ledger.

## First run

```bash
tldrack laya install     # the runtime venv, then the base model (about 800 MB), then serve it
tldrack laya status      # what is serving, and how far a first fine-tune is
```

Nothing is trained and nothing is configured: a first run serves the public base checkpoint, and
`/status` says so (`serving.base_only`). The same status counts the operator's own labels toward a
first fine-tune (`serving.first_fine_tune`: the REVIEW deck's AGREE / FLIP grades, 40 with 10 of each
class). Until then every verdict is the base model's, at the code's cut. Third-party licences:
`lab/NOTICE`.

## Routes (`/plugin/laya/…`)

Wire shapes: `schemas/src/decide.ts` (decide, ledger) and `schemas/src/laya-review.ts` (everything else).

| route | guard | what |
|---|---|---|
| `GET /status` | host | `state` (`not_installed` · `installing` · `stopped` · `starting` · `ready` · `failed`), served models, `model_ids`, device, last error, `thresholds` (swept cuts of the LOADED checkpoint), `serving` (`ready`, `base`, `base_only`, `fine_tunes`, `first_fine_tune`: `runtime.py` `_serving`) |
| `POST /decide` | host | a decide request (+ optional `ledger` tag, never forwarded) → Laya's answers + `decision_id`, `model_id`, `checkpoint` (a basename); `503 warming` / `not_installed`; `400 bad_request` names the field |
| `POST /react` | host | `{decision_id, action}` → `{ok, decision_id, action, duplicate}`; `409 unknown_decision`, `503 no_ledger` |
| `POST /landing` | host + native | where a rack verdict landed, `{decision_id, surface, class, held, running, trace}` → `{ok, decision_id, duplicate}`; `400 bad_request` names the field, `403 forbidden` for a browser or proxied caller, `409 unknown_decision`, `503 no_ledger` |
| `POST /start` · `/stop` · `/install` | policy | warm it / free its memory / install in the background: `uv sync --project runtime --locked`, then the base model (`install.py`) |
| `POST /restart` | policy | stop and start laya-serve, so a newly registered checkpoint loads |
| `GET /ops` | policy | AGENT RACK › LAYA in one body (`ops.py`); never 503s: missing params leave arrays empty and say why |
| `POST /lab` | policy + JSON | `{verb, surface}` → its lab job started now (`202`, `session`) or queued behind the running one (`202`, `queued: n`); `409 busy` only for that same verb running (below) |
| `GET /card` · `POST /card/publish` | policy (+ JSON) | the verifier card's preview, and `{digest}` → the reviewed card written into this checkout's `docs/` (`written`; `url` stays, empty: nothing is published anywhere) |
| `GET /review/queue` | policy | stats for every surface + up to `limit` (0–50) cards of one |
| `GET /review/item` | policy | one card by `surface` + `key` |
| `GET /review/recent` | policy | the newest `limit` (1–200, default 100) live verdicts of one surface, with the operator's answers; `after` (the last page's `summary.next`) starts strictly after that page's last row, and `offset` (0–100,000) skips that many more |
| `POST /review/answer` · `/review/undo` | policy + JSON | the operator's AGREE / FLIP / SKIP (one press may grade a wave), and UNDO |
| `POST /review/retest` | policy + JSON | the operator's blind second look at a text they graded, `{surface, key, of, open, needs?}` → `{retest, stats}`; a `retest` row, never an answer; `409 retest_stale` · `already_retested` · `not_due`. Owner: `laya_bridge/review.py` `retest` |
| `/review/rack` | in process only (`rack_grade(body, headers)`), never mounted | the agent rack's grade of one live verdict, `{decision_id, open}`, from the ⚠️'s own menu: an operator answer in stratum `rack`; `409 already_answered` keeps a standing OPEN or DONE answer, and a skip is replaced. The headers are those of the press, and pass the policy and JSON checks before a row is written. Owner: `laya_bridge/review.py` `rack`; who may call: `laya_bridge/plugin.py` |
| `judged_before(conversation, state_hash)` | in process only, never mounted | has the ledger already judged this agent-rack turn end (`true`/`false`, `null` when it cannot say): the rack asks before judging a turn end its bridge missed. Owner: `laya_bridge/ledger.py` `Ledger.judged`; why: tldrack `open_work.py` `ledger_judged` |

Anything else is `404 {error: "not found"}`.

## Guards

- Which entry sees headers, the two guards and the in-process pair: owned by `laya_bridge/plugin.py` — see the note there.
- The policy (the operator's origins, the bridge's own page, the dev server), and what counts as a native caller: owned by `laya_bridge/guard.py` — see the note there.
- The policy-route envelope (`error == message`): owned by `laya_bridge/envelope.py` — see the note there.

## The decision ledger

Files, modes, rotation, `KEEP_TEXT` and `RACK_SURFACES`: owned by `laya_bridge/ledger.py` — see the note there.
The landing record beside it (where each rack verdict landed, and why it is no tag field): owned by
`laya_bridge/landing.py` — see the note there.

## The REVIEW deck

The deck page (`/laya-review.html`) reads `/review/*`. Strata, cards and stats: owned by
`laya_bridge/review_data.py`; the operator's answer log: owned by `laya_bridge/review_log.py` — see
the notes there.

## Lab jobs

Each lab verb (`challenger-sweep`, `sweep`, `sweep-reset`, `score`, `promote`, `golden`, `compare`) runs one fixed
`laya-lab` command in tmux session `laya-<verb>`. One lab job runs at a time; a press while one runs waits in
`<data>/laya/lab-queue.json` and the job row reads `queued` with its place. The drain thread in `plugin.py`
(`LabDrain`) starts the next one when the running job ends, with nobody watching, and a queue survives a bridge
restart. `train` and `label-open-work` are never lab verbs. The argv table, the two spawners, the queue, the pane
marks and that guard: owned by `laya_bridge/lab_jobs.py` — see the note there.

## Settings

The operator's `settings.json` (the config directory), under `plugins.laya` (plugins/README.md § What the context carries). Every key is
optional, and the default is the safe one.

| Key | Default | Set |
|---|---|---|
| `evals_dir` | lab jobs start in tmux by themselves | an absolute directory holding an eval runner (`evals/cli.py`, `registry/`): each lab job is started through it, so it is also a run in that runner's ledger (`lab_jobs.py`) |
| `card_repo` | `voidcraft-world/tldrack` | the `owner/name` a verifier card's pull request goes to; a fork's operator names their own fork. laya-lab reads the same key from this file (`card_files.configured_repo`), and a card written for another repository is refused as stale |

Browser origins are the bridge's own setting (`allowed_origins`), not this plugin's: `guard.py` reads them from the
context.

## Where the data is

`<data>/laya/` (`paths.laya_dir` owns the order: `paths.json` `stores.laya`, `$TLDRACK_DATA_DIR/laya`, the
default `~/.local/share/tldrack/laya`). The plugin creates it at mount; laya-lab, from its own venv, resolves the same
folder.

## Pure modules

`rows`, `agreement`, `files`, `paths`, `wire`, `ledger`, `landing`, `envelope`, `review_surfaces`,
`review_log`, `label_budget`, `review_data`, `review`, `lab_files`, `lab_jobs`, `ops`, `challenger`, `card_files`,
`install` and `env_names` import ONLY the stdlib and each other:
laya-lab imports them from its own venv, which has no bridge SDK. So the shared rules — the state
key, κ, the operator-log fold, the label rules, the operator-label budget, every laya-lab file format — have one owner here,
and laya-lab never forks them. Only `guard`, `runtime` and `plugin` import `voidcraft_world_bridge`;
`ops.ops_state` takes the runtime's facts as arguments. Enforced by `tests/test_purity.py` and
`lab/tests/test_bridge_imports.py`.

## How it runs

- **Its own venv** (`runtime/`, `laya[serve]==0.4.19`): plugins are stdlib-only, so this one only
  spawns `laya-serve` and speaks HTTP to it.
- **Loopback + a per-spawn key**: `LAYA_HOST=127.0.0.1` (laya-serve defaults to 0.0.0.0), and the
  bearer key never leaves the plugin. Everything else reaches Laya through the bridge's guards.
- **English checkpoint only** at start (`LAYA_MODELS=english`); stops itself after 15 idle minutes.
- **Fine-tunes**: each surface's @champion in `<data>/laya/models.json` (written by laya-lab, `lab/`)
  is served by name — a caller pins one with `"model": "voidcraft-paste"`. A @challenger is never loaded:
  laya-lab's nightly pack scores it in shadow (`shadow/<surface>.jsonl`, read through `lab_files.read_shadow`).
- **File modes**: at mount the plugin tightens the operator's files under `laya/` to 0600 and their
  folders to 0700 (`files.tighten_params`), and logs each path it changed.

The why behind each of these is owned by `laya_bridge/runtime.py` and `laya_bridge/paths.py`.

## Enable

`plugins/laya` is in the bridge's default plugin list (plugins/README.md). On a bridge that lists its plugins by
hand, add the directory, restart the bridge, then run `tldrack laya install` (or `POST /install`).
