# How the agent rack works

<!-- The page behind the rack's ⌗ HOW button (AgentRackDiagram.vue) renders one tab per `## `
     section. This file is the diagram's ONE owner: add a plugin, a route or a hook to the rack
     and it goes here too. tests/unit/agent-rack-diagram.test.ts fails while a directory of
     plugins/ is missing from the Topology tab. -->

## Topology

Who starts what. Everything runs on your machine except the phone; nothing here is a cloud service.

```mermaid
flowchart LR
    subgraph machine["Your machine"]
        up["tldrack up<br/>starts what is down, never stops a bridge"]
        bridge["tldrack bridge :7682<br/>HTTP API + the built pages<br/>(voidcraft-world-bridge core)"]
        ttyd["ttyd :7681<br/>patched index page"]
        tmux["tmux server<br/>one session per agent"]
        claude["Claude Code panes<br/>per account CLAUDE_CONFIG_DIR"]
        codex["Codex panes<br/>--no-daemon"]
        agy["Antigravity panes (agy)"]
        subgraph plugins["plugins/"]
            rp["worktree · laya · agent-canvas<br/>coordinator · claude-skills · devices"]
            pl["phone-link :7683<br/>signed nonces · allow-listed routes"]
        end
    end
    subgraph pages["Pages (ui/dist, served at /)"]
        rack["index.html<br/>the rack: list + live terminal"]
        review["review.html<br/>Laya review"]
        more["diagram.html · office.html"]
    end
    host["A host application (optional)<br/>imports @tldrack/ui: a framed rack<br/>or a satellite window"]
    phone["Phone<br/>tailnet: 443 to ttyd, 8443 to bridge"]

    up -->|"starts in tmux, or asks a supervisor"| bridge
    bridge -->|"child process"| ttyd
    bridge -->|"/spawn via spawn_argv"| tmux
    ttyd -->|"tmux attach"| tmux
    tmux --> claude
    tmux --> codex
    tmux --> agy
    bridge --- plugins
    bridge -->|"GET /"| pages
    claude -->|"http hooks: POST /hook + pane header"| bridge
    codex -->|"hooks.json: POST /hook"| bridge
    agy -->|"hooks.json: POST /hook&agent=antigravity"| bridge
    rack -->|"/snapshot + SSE /events"| bridge
    rack -->|"iframe /ws"| ttyd
    review -->|"/plugin/laya/review/*"| bridge
    host -->|"its own poll, or the window bus"| bridge
    phone -->|"tailscale serve"| rack
    phone -->|"adb reverse tcp:7683 only, never a bridge port"| pl
    pl -->|"no Origin, no phone headers"| bridge
```

## Status pipeline

How one row decides busy / attention / free. Hooks are fast but miss things (Esc fires no hook);
the arbiter and the public CLI catch what they miss.

```mermaid
flowchart TD
    hooks["Claude hooks<br/>UserPromptSubmit · PreToolUse · PermissionRequest<br/>Notification · Stop · StopFailure<br/>SessionStart · SessionEnd · TaskCreated · TaskCompleted"]
    chooks["Codex hooks<br/>PreToolUse · PostToolUse · PermissionRequest<br/>Stop · Interrupt · SessionStart · SessionEnd"]
    ahooks["Antigravity hooks (agy)<br/>SessionStart · PreInvocation · PostToolUse · Stop<br/>never PreToolUse: empty stdout denies"]
    relay["Claude: http hook, X-VC-Pane = TMUX_PANE<br/>Codex + agy: curl relay<br/>2 s timeout · no decision"]
    panes["pane to session<br/>PaneSessionCache, 1 s"]
    ledger["permission ledger<br/>PermissionRequest rows<br/>outcome from the transcript"]
    store["StatusStore<br/>state · activity · model · task progress"]
    tasks["tasks.py fold<br/>TaskCreated / TaskCompleted ids<br/>TodoWrite fallback"]
    arbiter["StateArbiter tick 2 s<br/>private index sessions/PID.json<br/>+ ps work children"]
    public["public reconciler 15 s<br/>claude agents --json<br/>once per CLAUDE_CONFIG_DIR"]
    drift{"private index<br/>drifted?"}
    fallback["public mode for that account<br/>5 s · pid to tmux pane via ps ancestry"]
    judge["turn end: refine_turn_end<br/>+ open-work judge (laya plugin)"]
    next["next step (next_step.py)<br/>rules first, else a local model when one is configured<br/>a SUGGESTION: rows carry it in live mode only"]
    attention["attention policy<br/>attention.py: ⚠️ new · repeat · parked · ⏳ while children run<br/>stuck.py: same command failed 4× · 20 min silent"]
    out["/snapshot + SSE /events"]
    consumers["rack rows · the office"]
    presses["rack reports fired presses<br/>POST /next-step/event<br/>scored in next-step-log.jsonl"]
    phone["phone push (notify.py, when a topic is set)<br/>once per wait · never on done<br/>optional reviews as a digest"]
    signals["rack-metrics signals<br/>ASKED · HELD"]

    hooks --> relay
    chooks --> relay
    ahooks --> relay
    relay --> panes --> store
    relay --> tasks --> store
    relay --> ledger
    arbiter -->|"compare-and-set correction"| store
    public --> drift
    drift -->|no| arbiter
    drift -->|"yes: 2 ticks running"| fallback --> store
    store --> judge --> store
    store --> attention --> out
    out -->|"observe, O(1)"| next -->|"live: row next_step"| out
    store --> out --> consumers
    consumers --> presses --> next
    attention --> phone
    attention --> signals
    phone --> signals
```

## Launch

What happens between pressing a LAUNCH chip and a live row. Settings reads and writes
`/launch-directories`; a regular `/spawn` carries the selected saved `cwd`.
The directory contract: `bridge/tldrack/launch_directories.py`.

```mermaid
sequenceDiagram
    participant U as You
    participant R as Rack (Vue)
    participant B as tldrack
    participant T as tmux
    participant C as Claude or Codex
    U->>R: press a LAUNCH chip (+ NEW)
    R->>B: POST /spawn with profile id
    B->>B: resolve profile, build_inner (CLAUDE_CONFIG_DIR, SHED_ENV, --no-daemon)
    B->>T: new-session, stamp @vc_profile and @vc_lineage
    T->>C: zsh -l -i -c agent CLI
    C->>B: SessionStart hook (POST /hook)
    B-->>R: SSE snapshot with the new row
    R->>U: row appears, pane attaches over ttyd /ws
    C->>B: UserPromptSubmit, PreToolUse ... Stop
    B-->>R: busy, attention, free
```

## Close and reopen

A ✕ is one click, so it has an undo. The record a close drops lands on the bridge's closed list, and ↺ CLOSED brings one back where it was, conversation and all.

```mermaid
sequenceDiagram
    participant U as You
    participant R as Rack (Vue)
    participant B as tldrack
    participant T as tmux
    U->>R: ✕ on a row
    R->>B: POST /kill-session
    B->>T: kill-session
    B->>B: session record + label → closed-sessions.json
    B-->>R: snapshot carries closed: N
    R->>U: ↺ CLOSED N in the head
    U->>R: open it, press a row
    R->>B: GET /closed, then POST /reopen with the close id
    B->>T: new-session in the old cwd, claude --resume
    B-->>R: 200 with the session name
    R->>U: the pane focuses the reopened session
```

## Parent task groups

The parent keeps the task context. The worktree plugin counts reported milestones and publishes PRs without an LLM monitor.
The percentage measures milestones, not time. PRs have a separate opened/total count. Merging requires a later request.

```mermaid
flowchart TD
    parent["Parent session<br/>stable lineage UUID"]
    launch["A launcher script<br/>register fixed workers, then launch and seal"]
    workers["Worker sessions<br/>research → implement → validate"]
    report["task-group report<br/>ready = tested SHA + PR title/body<br/>or explicit blocker"]
    store["Worktree plugin<br/>SQLite task-group record"]
    github["The plugin's GitHub index<br/>manual PRs + merges<br/>exact branch / worker namespace"]
    meter["Parent strip in LIST + GRAPH<br/>milestone % · blockers · PRs opened/total<br/>pause / resume / retry"]
    gate{"Sealed + all ready<br/>auto PR on + no blockers?"}
    check["Check every managed worktree<br/>clean tree, tested head, published base<br/>reject local snapshot ancestry"]
    publish["Script: push exact tested commit<br/>find or create PR<br/>persist URL; recover after restart"]
    context["Parent context: worker results + PR URLs<br/>rack readouts; no terminal input"]
    merge["Operator later requests merge<br/>parent reads the saved group PR list"]
    parent --> launch --> workers --> report --> store
    store --> meter
    github -->|"reconcile; merged = delivered 100%"| store
    store --> gate
    gate -->|yes| check --> publish --> store
    gate -->|blocked or incomplete| meter
    store --> context --> parent
    context --> merge
```

## Missions

A mission is agents assigned to one task. The bridge tells each member who else is on it and where they are
working, through its hooks. Nothing is typed into a pane, and no LLM runs.

```mermaid
flowchart TD
    rack["Rack MISSIONS section<br/>start, add, role, note, close"]
    cli["tldrack mission<br/>an agent in its own pane"]
    store["MissionStore (bridge core)<br/>missions.json: members, roles, notes"]
    lineage["/snapshot rows<br/>a child inherits its parent's mission, once"]
    edits["PreToolUse Edit / Write<br/>touching areas, overlap warning<br/>in memory"]
    prompt["UserPromptSubmit answer<br/>the whole briefing"]
    tool["PreToolUse answer<br/>only what changed"]
    agent["Claude member"]
    rack -->|"POST /missions"| store
    cli -->|"POST /missions"| store
    lineage --> store
    edits --> store
    store --> prompt --> agent
    store --> tool --> agent
    store -->|"GET /missions poll"| rack
```

## Lead

A LEAD runs a planned feature through worker sessions and never does the work itself. The
bridge watches the workers at zero tokens and wakes the lead once per event. Only the operator answers a
decision, from the rack's DECISIONS card.

```mermaid
flowchart TD
    fork["LAUNCH fork › LEAD<br/>AUTO or a Claude chip"]
    launch["POST /lead/launch<br/>profile, resume?"]
    lead["Lead session<br/>main checkout, edits and forks refused"]
    store["rack.sqlite (bridge core)<br/>runs · steps · decisions"]
    worker["Worker sessions<br/>one worktree per step, on the worker model"]
    group["worktree task group<br/>open membership, a PR per step"]
    watcher["watcher<br/>zero tokens"]
    card["Rack DECISIONS card<br/>KEEP · options or ANSWER · DEFER"]
    secret["GET /decision-secret<br/>browser fetch only"]
    fork --> launch --> lead
    lead -->|"tldrack lead …"| store
    lead -->|"lead spawn"| worker --> group
    worker -->|"lead ask"| store
    group --> watcher
    store --> watcher
    watcher -->|"one wake per event, typed when the pane is safe"| lead
    store -->|"GET /lead/state poll"| card
    secret --> card
    card -->|"POST /inbox/answer · defer"| store
```

## Session ledger

Every conversation kept past the agent CLI's own cleanup, and the human decisions in it as rows. It
reads the transcripts on this machine into the bridge's data directory. Nothing leaves the machine.

```mermaid
flowchart LR
    claude["Claude transcripts<br/>every account, subagents"]
    codex["Codex rollouts<br/>and auto-review threads"]
    side["side ledgers<br/>permissions · Laya · rack activity · pushes"]
    timer["bridge timer, 15 min<br/>nice'd child process"]
    ingest["ledger ingest<br/>incremental, one at a time"]
    raw["raw archive<br/>zstd frames, never deleted"]
    db["ledger.sqlite<br/>turns · events · tool calls"]
    relay["relay pass<br/>agent-typed prompts → machine"]
    links["Laya link pass<br/>each verdict → its conversation + turn<br/>laya_links, every pass"]
    use["export pairs · decisions<br/>Laya, the judge"]
    history["Rack HISTORY<br/>guarded search + conversation reads<br/>local / tailnet, no-store"]
    review["Laya review page<br/>GET /ledger/threads, one join a page<br/>thread bands"]
    find["Rack FIND box<br/>GET /ledger/rack-search<br/>each live row's thread + unread tail"]
    timer --> ingest
    claude --> ingest
    codex --> ingest
    side --> ingest
    ingest --> raw
    ingest --> db --> relay --> db
    db --> links --> db
    db --> use
    db --> history
    db --> review
    db --> find
    claude -.->|"the tail the ingest has not read"| find
    codex -.-> find
    review -.->|"pending ids: POST /ledger/ingest, once a minute"| timer
```

## Safety

Rack interaction history: `AgentRackComponent` → `metric` transport action → guarded
`POST /rack-metrics/event` → local `rack-metrics.sqlite`. `GET /rack-metrics` reads it back; settings
and experiment markers use `/rack-metrics/settings`.

Suggested next step: `AgentRackComponent` → `next_step` transport action (a REPORT, never an act) →
guarded `POST /next-step/event` → `next-step-log.jsonl`. [DO IT] presses the row's own control, so its
arm and refusals still run. `GET /next-step` reads the state and `POST /next-step/settings` switches
the mode.

Settings: `settings.html` reads `GET /doctor` (the checks `tldrack doctor` prints, as JSON) and
`GET /features`, and writes guarded `POST /features` → `features.json`. A switch can only turn a part of
this bridge off or back on, never name a path or load code: `bridge/tldrack/features.py`. What one browser
draws (the 3D office, motion) stays in that browser: `ui/src/config/rack-prefs.ts`.

Launch defaults: `GET/POST /profile-launch-settings`, owned by `bridge/tldrack/profile_launch_settings.py` — see the note there.

Update: the rack's ⚙ dot and the Settings UPDATE card read `GET /update` (the checkout against its
remote, the page against the checkout, the running code against the disk). Guarded
`POST /update {"action"}` fetches, or fast-forwards the checkout, rebuilds `ui/dist` and restarts the
bridge in place. The request names an action and nothing else: `bridge/tldrack/update.py`.

What is guarded, and where. Hooks only report; deciding stays with you or a deterministic rule.

```mermaid
flowchart LR
    subgraph agent["Inside an agent session"]
        tool["a tool call"]
        perm["permission dialog<br/>PermissionRequest relays, never decides"]
    end
    subgraph br["tldrack bridge"]
        hostg["Host + Origin guards<br/>every route, plugins cannot widen"]
        loop["loopback bind<br/>127.0.0.1 only"]
        ttydO["ttyd -O<br/>origin-checked WebSocket"]
    end
    subgraph remote["Remote"]
        tail["tailnet only<br/>tailscale serve 443 / 8443"]
        pin["?remote= names a pinned host only<br/>(VITE_ALLOWED_REMOTE_HOSTS)"]
        push["phone push<br/>pointer only, no content"]
    end

    tool --> perm
    perm -->|"POST /hook: a report"| hostg
    pin --> tail
    tail --> hostg
    hostg --> loop
    loop --> ttydO
    push -.->|"you open the rack"| tail
```
