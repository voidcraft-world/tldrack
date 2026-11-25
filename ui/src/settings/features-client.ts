/**
 * The page's side of the bridge's feature switches: `GET /features`, `POST /features`. What a
 * switch is, the two kinds (`now`, `restart`) and why a page may only ever switch a part off or
 * back on: owned by `bridge/tldrack/features.py` — see the note there.
 *
 * ONE BAD ROW DOES NOT BLANK THE LIST. A row this page cannot read is dropped and counted
 * (`unreadable`), so a switch a newer bridge describes differently costs that one row, and the
 * page says how many it left out. The envelope itself is strict: no `features` array is no view.
 *
 * `key` is opaque here. The page sends back exactly the string it was given and never builds one.
 */
import { z } from 'zod';

import { bridge_read, bridge_write, type BridgeResult } from './bridge-call.ts';

export const FEATURE_APPLIES = ['now', 'restart'] as const;
export type FeatureApplies = (typeof FEATURE_APPLIES)[number];

const sentence = z.string().max(600);
const feature_schema = z.object({
    key: z.string().min(1).max(120),
    group: z.string().min(1).max(40),
    title: z.string().min(1).max(120),
    does: sentence,
    cost: sentence,
    without: sentence,
    on: z.boolean(),
    applies: z.enum(FEATURE_APPLIES),
    pending: z.boolean(),
    lite: z.boolean(),
});
export type Feature = z.infer<typeof feature_schema>;

export interface FeaturesView {
    features: Feature[];
    /** The bridge's own sentence when its stored switches were unreadable, else null. */
    error: string | null;
    /** Rows the bridge sent that this page could not read. */
    unreadable: number;
}

const features_schema = z
    .object({ features: z.array(z.unknown()).max(500), error: z.string().max(600).nullish() })
    .transform((raw): FeaturesView => {
        const features: Feature[] = [];
        const seen = new Set<string>();
        for (const row of raw.features) {
            const parsed = feature_schema.safeParse(row);
            // A repeated key would give two switches one answer: the first row stands.
            if (!parsed.success || seen.has(parsed.data.key)) continue;
            seen.add(parsed.data.key);
            features.push(parsed.data);
        }
        return { features, error: raw.error || null, unreadable: raw.features.length - features.length };
    });

/** A `/features` body → the view, or null when it is not one. */
export function parse_features(body: unknown): FeaturesView | null {
    const parsed = features_schema.safeParse(body);
    return parsed.success ? parsed.data : null;
}

/** The groups the bridge names today, in page order. A group it adds later is drawn after them, under its own name. */
const KNOWN_GROUPS: ReadonlyArray<{ id: string; title: string; about: string }> = [
    { id: 'bridge', title: 'The bridge', about: 'Loops the bridge runs by itself. A change lands on the loop’s next tick.' },
    { id: 'plugins', title: 'Plugins', about: 'A plugin is mounted when the bridge starts, so a change here waits for the next start.' },
];

export interface FeatureGroup {
    id: string;
    title: string;
    about: string;
    features: Feature[];
}

export function group_features(features: readonly Feature[]): FeatureGroup[] {
    const groups: FeatureGroup[] = KNOWN_GROUPS.map((group) => ({ ...group, features: [] }));
    for (const feature of features) {
        let group = groups.find((candidate) => candidate.id === feature.group);
        if (!group) {
            group = { id: feature.group, title: feature.group, about: '', features: [] };
            groups.push(group);
        }
        group.features.push(feature);
    }
    return groups.filter((group) => group.features.length > 0);
}

/** How a restart is spelled. A restart is the operator's call: the page shows this and never runs it. */
export const RESTART_COMMAND = 'tldrack kill && tldrack up';

/** True for a switch whose saved answer the running bridge has not taken yet: the row then shows `RESTART_COMMAND`. */
export function waits_for_restart(feature: Feature): boolean {
    return feature.applies === 'restart' && feature.pending;
}

/** LITE's half for the bridge: every switch the bridge marks `lite`, off. */
export function lite_changes(features: readonly Feature[]): Record<string, boolean> {
    return Object.fromEntries(features.filter((feature) => feature.lite).map((feature) => [feature.key, false]));
}

/** DEFAULTS' half for the bridge: every switch on, which is a bridge with no stored switches. */
export function default_changes(features: readonly Feature[]): Record<string, boolean> {
    return Object.fromEntries(features.map((feature) => [feature.key, true]));
}

/** How many of `changes` would move a switch: 0 means the preset is already in place. */
export function changes_needed(features: readonly Feature[], changes: Readonly<Record<string, boolean>>): number {
    return features.filter((feature) => feature.key in changes && changes[feature.key] !== feature.on).length;
}

export function fetch_features(): Promise<BridgeResult<FeaturesView>> {
    return bridge_read('/features', features_schema, 'a feature list');
}

/** Set switches by key. The reply is the whole view after the write. */
export function set_features(changes: Readonly<Record<string, boolean>>): Promise<BridgeResult<FeaturesView>> {
    return bridge_write('/features', { set: changes }, features_schema, 'a feature list');
}
