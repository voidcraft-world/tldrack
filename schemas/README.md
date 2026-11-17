# @tldrack/schemas

The zod contracts that tldrack's bridge, plugins and pages agree on. Each wire the rack reads has one TypeScript
owner here. The Python side keeps a twin of each, and `tests/twins.test.ts` reads those twins so the two cannot
drift apart.

```ts
import { decideRequestSchema, read_systemone_response } from '@tldrack/schemas/decide.ts';
import { benchResultsSchema } from '@tldrack/schemas/bench-results';
```

Every module imports `zod` and its own siblings, nothing else, so a browser page, a Node CLI or a host's server
can load any of them. Responses use zod's default strip, so a newer bridge never breaks an older page. Requests
and on-disk rows are strict.

| Module | Holds |
|---|---|
| `decide` | Typed decisions: questions, answers, the `/v1/systemone` reader, surface names, the rack's surfaces, the Laya ledger and landing rows |
| `laya-review` | The Laya REVIEW deck, its RECENT list, OPS state and the operator's answer log |
| `bench-results` | One eval results file, `bench/results/<date>.json`. Aggregates only |
| `agent-history` | The session ledger's search, conversations and REVIEW thread join |
| `lead` | LEAD runs, steps and the decision inbox |
| `next-step` | The suggested next step on a finished turn |
| `resource-lease` | The lease on a machine's GPU memory, Claude login and CPU |
| `closed-sessions` | Recent closes the rack can reopen |
| `missions` | Missions and their members |
| `rack-metrics` | Local interaction measurements |
| `session-lineage` | Spawn identity |
| `task-groups` | Worktree task groups |
| `topic-labels` | The NAME card's verdicts on a session's auto name |

## Surfaces belong to the host

A decision surface is the feature that asks. The package names the rack's own surfaces (`agent_open_work`,
`coordinator`). The wire, the ledger and the review deck accept any surface whose name matches
`DECIDE_SURFACE_NAME`. A host that adds surfaces keeps its own registry of them: their labels, where each
one runs and which provider answers it. That registry imports these types.

## The fixture

`bench/results/fixture.json` holds invented numbers for building the EVALS page before a real bench has run.
`scripts/bench-fixture.ts` writes it from a seeded generator, and a test checks that the committed file
matches the generator's output. To change the fixture, edit the generator, never the JSON.

```bash
npm test                        # node --test tests/*.test.ts
npm run typecheck               # tsc, no emit
node scripts/bench-fixture.ts   # rewrite the fixture
```
