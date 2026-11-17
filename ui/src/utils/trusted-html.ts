/**
 * Every place this app hands a string to the HTML parser, in one file.
 *
 * ── What Trusted Types actually buys, stated honestly ────────────────────────
 *
 * Two of the three policies below are PASS-THROUGHS. That looks like theatre
 * until you notice what the browser does with the rest of the app: under
 * `require-trusted-types-for 'script'`, an `innerHTML` or `srcdoc` assignment
 * that did NOT come through a named policy throws. So the value is not in what
 * these functions compute, it is in the fact that the set of sinks becomes
 * finite, named, and greppable — and that a NEW one added next year fails
 * loudly instead of quietly joining the attack surface.
 *
 * That is the defect this exists to prevent: saved text assigned to `innerHTML`
 * executes as markup in the page's own origin, and nothing catches it but a
 * person reading the file. A policy list is a thing a machine can check.
 *
 * ── Why three policies and not one ───────────────────────────────────────────
 *
 * Because they carry different trust, and one name would hide that:
 *
 *   `prose_html`   markdown that a sanitizer has already run through
 *                  DOMPurify, and nothing else. Every caller, and
 *                  `markdown-sanitize.test.ts` fails on a new one:
 *                  - `laya-review/agent-markdown.ts` wraps the output of its
 *                    own private DOMPurify allowlist (agent messages).
 *                  - `agent-rack-diagram/AgentRackDiagram.vue` wraps the output of
 *                    `render_prose_fragment` (the diagram page's own markdown).
 *                  It cannot simply re-sanitize here: `markdown/markdown-doc-render.ts`
 *                  splices mermaid SVG in AFTER sanitizing, so the sanitizing
 *                  belongs to each pipeline, not to this file.
 *
 *   `static_html`  markup this app wrote as a literal — icon paths, a `<span>`
 *                  with a fixed label. If a caller ever interpolates a value
 *                  into one of these, that is the bug; escape it first.
 *
 *   `embed_html`   arbitrary user HTML going into an iframe that is sandboxed
 *                  WITHOUT `allow-same-origin`, so it lands in an opaque origin
 *                  and cannot reach this one. The trust is in the sandbox
 *                  attribute, not in the markup, and the separate name is what
 *                  keeps that from being forgotten.
 *
 * A host that serves these modules under a `trusted-types` CSP must list the
 * three names (`tldrack-sanitized`, `tldrack-static`, `tldrack-embed`), or the
 * browser refuses to create them. Adding a policy here means adding it there.
 *
 * ── Why the return type is `string` ──────────────────────────────────────────
 *
 * At runtime these return a `TrustedHTML` object where the browser supports it
 * and a plain string where it does not (Safari and Firefox, as of 2026-09).
 * Both are assignable to `innerHTML` and `srcdoc`, whose DOM typings say
 * `string`, so the cast keeps every call site a plain assignment instead of
 * spreading feature detection across a dozen files.
 */

/**
 * Escape a value for interpolation into an HTML string. The one owner.
 *
 * `"` is escaped as well as the three structural characters, so the result is
 * safe inside a double-quoted attribute and not only in text position.
 */
export function escape_html(text: string): string {
    return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

type PolicyLike = { createHTML: (input: string) => unknown };

function make_policy(name: string): PolicyLike | null {
    const factory = (globalThis as { trustedTypes?: { createPolicy: (n: string, r: PolicyLike) => PolicyLike } }).trustedTypes;
    if (!factory) return null;
    try {
        return factory.createPolicy(name, { createHTML: (input: string) => input });
    } catch (error) {
        // A duplicate name, or a name missing from the CSP allowlist. Neither is
        // worth breaking the page over: without a policy these fall back to
        // returning the string, which is exactly today's behaviour.
        console.warn(`[trusted-html] could not create the "${name}" policy:`, error);
        return null;
    }
}

let prose_policy: PolicyLike | null | undefined;
let static_policy: PolicyLike | null | undefined;
let embed_policy: PolicyLike | null | undefined;

function through(policy: PolicyLike | null, markup: string): string {
    return policy ? (policy.createHTML(markup) as unknown as string) : markup;
}

/**
 * Markdown already sanitized by DOMPurify. Owners of the sanitizing:
 * `markdown/markdown-doc-render.ts` and `laya-review/agent-markdown.ts`.
 */
export function prose_html(sanitized: string): string {
    if (prose_policy === undefined) prose_policy = make_policy('tldrack-sanitized');
    return through(prose_policy, sanitized);
}

/** Markup this app wrote as a literal. Never pass an interpolated value. */
export function static_html(markup: string): string {
    if (static_policy === undefined) static_policy = make_policy('tldrack-static');
    return through(static_policy, markup);
}

/**
 * User HTML bound for an iframe sandboxed WITHOUT `allow-same-origin`.
 * Passing this to a frame that is same-origin with the page is the one way to
 * misuse it, and would be XSS in the page's own origin.
 */
export function embed_html(markup: string): string {
    if (embed_policy === undefined) embed_policy = make_policy('tldrack-embed');
    return through(embed_policy, markup);
}

/**
 * Decode HTML entities in a text string WITHOUT touching an HTML sink.
 *
 * The idiom this replaces — assigning to a detached `<textarea>.innerHTML` and
 * reading `.value` — is inert (a detached textarea executes nothing) but it is
 * still an `innerHTML` write, so Trusted Types refuses it and the feature dies
 * for a reason that has nothing to do with the risk. `DOMParser` never runs
 * script for `text/html` and is not a Trusted Types sink, so it does the same
 * job with none of the argument.
 */
export function decode_html_entities(text: string): string {
    if (!text.includes('&')) return text;
    try {
        return new DOMParser().parseFromString(text, 'text/html').documentElement.textContent ?? text;
    } catch {
        return text;
    }
}
