import { rack_metric_event_schema, type RackMetricEvent } from '@tldrack/schemas/rack-metrics.ts';

type MetricContext = Pick<RackMetricEvent, 'session' | 'state' | 'mark' | 'fleet_size' | 'context_left' | 'model' | 'profile'>;
const ACTIONS = new Set(['select', 'kill', 'rename', 'new', 'worktree', 'pr', 'cleanup', 'dismiss_open_work', 'snooze_open_work', 'confirm_open_work', 'reopen', 'restore', 'group_control', 'lead']);

function metric_id(): string {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
    // A page served over plain HTTP may lack randomUUID. These are deduplication ids, never auth tokens.
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, char => {
        const value = Math.floor(Math.random() * 16);
        return (char === 'x' ? value : (value & 3) | 8).toString(16);
    });
}

/** One recorder per rendered Rack. Only explicit actions count; payloads merely change context.
 * Exposure is foreground visibility, never claimed reading time. A delayed timer (>20 s) drops
 * the interval rather than booking sleep as attention. The bridge unions overlapping windows.
 */
export function create_rack_metric_recorder(deps: {
    emit: (event: RackMetricEvent) => void;
    context: (session?: string) => MetricContext;
    focused: () => string | null;
    visible: () => boolean;
    source: RackMetricEvent['source'];
    now?: () => number;
    monotonic?: () => number;
    id?: () => string;
}) {
    const now = deps.now ?? Date.now;
    const monotonic = deps.monotonic ?? (() => performance.now());
    const id = deps.id ?? metric_id;
    const client = id();
    let prior: { at: number; context: MetricContext } | null = null;

    function record(kind: RackMetricEvent['kind'], action: RackMetricEvent['action'], context: MetricContext,
        duration_ms = 0, reselect = false): void {
        const parsed = rack_metric_event_schema.safeParse({ ...context, id: id(), client, at: now(),
            kind, action, source: deps.source, duration_ms, reselect });
        if (parsed.success) {
            try { deps.emit(parsed.data); } catch { /* Measurement must never block a terminal action. */ }
        }
    }

    function tick(): void {
        const at = monotonic();
        const context = deps.context();
        const visible = deps.visible();
        if (prior) {
            const elapsed = Math.round(at - prior.at);
            if (elapsed > 0 && elapsed <= 20_000) record('exposure', 'exposure', prior.context, elapsed);
        }
        prior = visible && context.session ? { at, context } : null;
    }

    return {
        tick,
        stop(): void { tick(); prior = null; },
        action(action: string, session?: string): void {
            if (!ACTIONS.has(action)) return;
            // No action arguments: prompts, rename values and group controls may contain prose.
            const context = deps.context(session);
            if (action === 'select' && !context.session) return;
            if (action !== 'select' && !session) {
                context.session = null;
                context.state = 'unknown';
                context.mark = 'none';
                context.context_left = null;
                context.model = null;
                context.profile = null;
            }
            record(action === 'select' ? 'select' : 'action', action as RackMetricEvent['action'],
                context, 0, action === 'select' && context.session === deps.focused());
        },
    };
}

/** Bounded, in-memory delivery. Retry ids survive a lost HTTP response; no await on navigation.
 * Transient failures retry on the next interaction/heartbeat. A closed tab can lose queued data;
 * this is measurement, never a reason to hold the operator in a terminal. No offline backfill.
 */
export function create_metric_delivery(post: (event: RackMetricEvent) => Promise<boolean>) {
    const queue: RackMetricEvent[] = [];
    let running = false;
    async function drain(): Promise<void> {
        if (running) return;
        running = true;
        try {
            while (queue.length) {
                const event = queue[0]!;
                let ok = false;
                try { ok = await post(event); } catch { /* Retry on the next event. */ }
                if (!ok) break;
                if (queue[0] === event) queue.shift();
            }
        } finally { running = false; }
    }
    return (value?: string | null): void => {
        let raw: unknown;
        try { raw = JSON.parse(value ?? ''); } catch { return; }
        const parsed = rack_metric_event_schema.safeParse(raw);
        if (!parsed.success) return;
        if (queue.length >= 120) queue.splice(running ? 1 : 0, 1);
        queue.push(parsed.data);
        void drain();
    };
}
