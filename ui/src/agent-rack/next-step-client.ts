/**
 * The rack's half of the suggested next step: report what the operator did about a suggestion.
 * What a suggestion is, and why nothing here can run one: owned by
 * `bridge/tldrack/next_step.py` — see the note there.
 *
 * Runs in the transport OWNER, like metric delivery, so a framed rack's reports ride the owner's
 * local-network permission. One sequential queue per
 * owner keeps a press ahead of anything sent after it.
 */
import { local_fetch } from '../engine/terminal/local-network-gate.ts';
import { next_step_event_schema, type NextStepEvent } from '@tldrack/schemas/next-step.ts';

/** The rack page encodes an event as the transport's one string `value`. */
export function encode_next_step_event(event: NextStepEvent): string {
    return JSON.stringify(event);
}

/** `value` back into an event, re-validated: it crossed a postMessage or a BroadcastChannel. */
export function parse_next_step_event(value: string | null | undefined): NextStepEvent | null {
    let raw: unknown;
    try { raw = JSON.parse(value ?? ''); } catch { return null; }
    const parsed = next_step_event_schema.safeParse(raw);
    return parsed.success ? parsed.data : null;
}

const MAX_QUEUED = 60;

/** `(session, value) => void`: queue one report. Never throws, never blocks a control: a lost report
 *  costs a log row, and a 4xx (an older bridge, a refused event) is dropped rather than retried. */
export function create_next_step_delivery(
    base_url: (path: string) => string,
    post: (url: string, body: string) => Promise<Response> = (url, body) => local_fetch(url, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body,
        signal: AbortSignal.timeout(2500), keepalive: true,
    }),
) {
    const queue: string[] = [];
    let running = false;
    async function drain(): Promise<void> {
        if (running) return;
        running = true;
        try {
            while (queue.length) {
                const body = queue[0]!;
                let done = false;
                try {
                    const response = await post(base_url('/next-step/event'), body);
                    done = response.ok || (response.status >= 400 && response.status < 500);
                } catch { /* the bridge blinked: retried on the next report */ }
                if (!done) break;
                queue.shift();
            }
        } finally { running = false; }
    }
    return (session: string | null | undefined, value: string | null | undefined): void => {
        const event = parse_next_step_event(value);
        if (!session || !event) return;
        if (queue.length >= MAX_QUEUED) queue.shift();
        queue.push(JSON.stringify({ session, ...event }));
        void drain();
    };
}
