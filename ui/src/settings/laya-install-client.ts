/**
 * The SETUP tab's Laya card: is the local review model installed and serving, and the two buttons
 * that get it there. The plugin's routes and states: owned by `plugins/laya/README.md` § Routes and
 * `plugins/laya/laya_bridge/runtime.py` `status` — see the notes there.
 *
 *   GET  /plugin/laya/status    the state and what is served
 *   POST /plugin/laya/install   202 + status: the runtime venv, then the base model, in the background
 *   POST /plugin/laya/start     200 + status: load the model now
 *
 * WHAT THE STATUS DOES NOT CARRY. An install in progress reports `installing` and nothing more: the
 * plugin writes its log to a file and puts its tail in `error` only when the install FAILED. So the
 * card says what the install is doing in its own words, and shows `error` when there is one.
 *
 * A 404 is the bridge host's own answer for a plugin it never mounted: the card then says the
 * plugin is off, which the FEATURES tab is where to change.
 *
 * The rack has its own, tolerant reader of the same status (`decide/laya-local-client.ts`); this
 * one reads the two fields that one drops (`installed`, `serving`) and keeps a failure's kind.
 */
import { z } from 'zod';

import { LAYA_PLUGIN, LAYA_STATES, type LayaState } from '../decide/laya-local-client.ts';
import { bridge_read, bridge_write, type BridgeFailure, type BridgeResult } from './bridge-call.ts';

const serving_schema = z.object({
    base: z.string().max(200).optional(),
    base_only: z.boolean().optional(),
    fine_tunes: z.array(z.string().max(200)).max(100).optional(),
});

const status_schema = z.object({
    state: z.enum(LAYA_STATES),
    installed: z.boolean().optional(),
    error: z.string().max(8000).nullish(),
    device: z.string().max(80).nullish(),
    models: z.array(z.string().max(200)).max(200).catch([]),
    // Additive on the plugin's side, and only a caption here: a shape this page cannot read is no caption.
    serving: serving_schema.optional().catch(undefined),
});
export type LayaInstallStatus = z.infer<typeof status_schema>;

/** A `/status` body → the fields the card reads, or null when it is not a status. */
export function parse_laya_install_status(body: unknown): LayaInstallStatus | null {
    const parsed = status_schema.safeParse(body);
    return parsed.success ? parsed.data : null;
}

/** The plugin answered, the plugin is not mounted, or the read failed. */
export type LayaCard =
    | { kind: 'status'; status: LayaInstallStatus }
    | { kind: 'off' }
    | { kind: 'failed'; failure: BridgeFailure };

export function laya_card_of(result: BridgeResult<LayaInstallStatus>): LayaCard {
    if (result.ok) return { kind: 'status', status: result.data };
    return result.kind === 'missing' ? { kind: 'off' } : { kind: 'failed', failure: result };
}

/** While Laya is on its way somewhere the card asks again; in every other state it is still. */
export const LAYA_POLL_MS = 2_000;

export function laya_should_poll(state: LayaState): boolean {
    return state === 'installing' || state === 'starting';
}

/** What the base model weighs, in the words the install button and the README use. */
export const LAYA_DOWNLOAD = 'about 800 MB';

export type LayaTone = 'ok' | 'warn' | 'busy' | 'idle';

export interface LayaCardView {
    /** The state as a short tag. */
    tag: string;
    tone: LayaTone;
    /** One sentence: what this state means and what to press. */
    line: string;
    /** The plugin's own error text, shown verbatim in a well. */
    error: string | null;
    install: boolean;
    start: boolean;
}

function served(status: LayaInstallStatus): string {
    const where = status.device ? ` on ${status.device}` : '';
    const fine_tunes = status.serving?.fine_tunes ?? [];
    if (fine_tunes.length > 0) return `Serving${where}: ${fine_tunes.join(', ')}.`;
    return `Serving the public base model${where}. It is every verdict until a first fine-tune of your own.`;
}

/** The card for one status. Pure: every word the card shows about a state is decided here. */
export function laya_card_view(status: LayaInstallStatus): LayaCardView {
    const error = status.error || null;
    switch (status.state) {
        case 'not_installed':
            return {
                tag: 'NOT INSTALLED',
                tone: 'warn',
                line: `Install it to have finished turns marked ✅ or ⚠️. The download is ${LAYA_DOWNLOAD}.`,
                error,
                install: true,
                start: false,
            };
        case 'installing':
            return {
                tag: 'INSTALLING',
                tone: 'busy',
                line: `Installing the runtime, then the base model (${LAYA_DOWNLOAD}). This takes a few minutes, and the page follows it.`,
                error,
                install: false,
                start: false,
            };
        case 'stopped':
            return {
                tag: 'STOPPED',
                tone: 'idle',
                line: 'Installed, and not loaded. It loads by itself at the next finished turn; START loads it now.',
                error,
                install: false,
                start: true,
            };
        case 'starting':
            return { tag: 'STARTING', tone: 'busy', line: 'Loading the model.', error, install: false, start: false };
        case 'ready':
            return { tag: 'READY', tone: 'ok', line: served(status), error, install: false, start: false };
        case 'failed':
            return {
                tag: 'FAILED',
                tone: 'warn',
                line: 'The last install or start failed. START tries the model again; INSTALL repeats the install.',
                error,
                install: true,
                start: status.installed !== false,
            };
    }
}

const BASE = `/plugin/${LAYA_PLUGIN}`;

/** The raw result, so the caller can tell the page whether the bridge answered; `laya_card_of` reads it. */
export function fetch_laya_status(): Promise<BridgeResult<LayaInstallStatus>> {
    return bridge_read(`${BASE}/status`, status_schema, 'a Laya status');
}

export type LayaLifecycle = 'install' | 'start';

/** INSTALL or START. The reply is the status after the press, so the card moves without a second read. */
export function laya_lifecycle(action: LayaLifecycle): Promise<BridgeResult<LayaInstallStatus>> {
    return bridge_write(`${BASE}/${action}`, {}, status_schema, 'a Laya status');
}
