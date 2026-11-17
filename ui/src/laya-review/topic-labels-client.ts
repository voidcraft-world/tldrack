/**
 * The NAME card's side of the bridge's `/topic-labels`: the wire is `schemas/src/topic-labels.ts`, the rows and why
 * a ✎ rename counts as a verdict are `bridge/tldrack/topic_labels.py`. Every body is `safeParse`d
 * here, so the card never reads an unchecked shape.
 *
 * Never throws: data, or one line for beside the card. A 404 is a bridge older than the route.
 */
import {
    topicLabelRequestSchema,
    topicLabelResultSchema,
    topicLabelsViewSchema,
    type TopicLabelRequest,
    type TopicLabelResult,
    type TopicLabelsView,
} from '@tldrack/schemas/topic-labels.ts';

import { get_bridge_json } from './bridge-get.ts';
import { BRIDGE_PREDATES_MESSAGE, post_bridge_json } from './bridge-post.ts';
import { bridge_origin } from '../config/bridge-origin.ts';
import type { ReviewSession } from './open.ts';

/** Loopback answers in milliseconds; this only bounds a hung bridge. */
const TIMEOUT_MS = 5_000;
const MESSAGE_CHARS = 160;
const INVALID = 'topic-labels: invalid bridge response';

export type TopicCall<T> = { ok: true; data: T } | { ok: false; status: number; error: string; hint?: string };

function topic_labels_url(): string {
    return `${bridge_origin()}/topic-labels`;
}

export async function load_topic_labels(session: ReviewSession): Promise<TopicCall<TopicLabelsView>> {
    const query = new URLSearchParams({ session: session.name });
    if (session.since !== null) query.set('since', String(session.since));
    const got = await get_bridge_json(`${topic_labels_url()}?${query}`, TIMEOUT_MS);
    if (!got.ok) {
        const error = got.status === 404 ? BRIDGE_PREDATES_MESSAGE : got.error;
        return { ok: false, status: got.status, error, ...(got.hint ? { hint: got.hint } : {}) };
    }
    const parsed = topicLabelsViewSchema.safeParse(got.body);
    return parsed.success ? { ok: true, data: parsed.data } : { ok: false, status: 200, error: INVALID };
}

export async function post_topic_label(request: TopicLabelRequest): Promise<TopicCall<TopicLabelResult>> {
    const checked = topicLabelRequestSchema.safeParse(request);
    if (!checked.success) return { ok: false, status: 0, error: 'topic-labels: invalid request' };
    const sent = await post_bridge_json(topic_labels_url(), checked.data, TIMEOUT_MS, MESSAGE_CHARS);
    if (sent.status === 404 || sent.status === 426) return { ok: false, status: sent.status, error: BRIDGE_PREDATES_MESSAGE };
    if (!sent.ok) return { ok: false, status: sent.status, error: sent.message };
    const parsed = topicLabelResultSchema.safeParse(sent.body);
    return parsed.success ? { ok: true, data: parsed.data } : { ok: false, status: sent.status, error: INVALID };
}
