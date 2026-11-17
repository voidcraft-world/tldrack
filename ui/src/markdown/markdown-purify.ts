// markdown-purify.ts — what every markdown renderer in this app shares about SAFETY: the DOMPurify config for
// markdown a person or an agent wrote, and the task-list box that keeps a form control out of the markup.
//
// Its own module, with no runtime imports, for the reason `first-heading.ts` gives: anything that wants these two
// without marked and highlight.js (the Laya review page's agent renderer, `laya-review/agent-markdown.ts`) imports
// them from here. `markdown-pipeline.ts` re-exports both, so its importers are unchanged.
import type { Config as PurifyConfig } from 'dompurify';
import type { Tokens } from 'marked';

/**
 * THE DOMPurify config for markdown a person or an agent wrote: a `v-html` sink, the prose
 * iframe document, and every markmap node (`markdown-doc-render.ts` runs them all through
 * it). Here, not there, so `node --test` can pin it.
 *
 * Markdown can come from someone else (a shared document, an agent's proposal), so it is an
 * attack surface, not a scratchpad. DOMPurify's default already removes script. What this adds:
 * - **No form controls, `<style>` or `<dialog>`.** A fake "session expired, sign in" form
 *   or a full-page overlay in a document is phishing that needs no script. Task-list boxes
 *   are a glyph instead of an `<input>` (`render_task_box`).
 * - **No `style`, popover or invoker attributes, and no form actions.** Inline style can
 *   paint a fake UI over the whole panel. `popover`/`command` let markup open or fire
 *   page controls without script.
 * - **`SANITIZE_NAMED_PROPS`** prefixes `id`/`name` values, so a document cannot clobber
 *   `document.<name>` globals the page reads.
 */
export const MARKDOWN_PURIFY_CONFIG: PurifyConfig = Object.freeze({
    FORBID_TAGS: ['form', 'input', 'button', 'select', 'textarea', 'style', 'dialog'],
    FORBID_ATTR: ['style', 'popover', 'popovertarget', 'popovertargetaction', 'command', 'commandfor', 'action', 'formaction'],
    SANITIZE_NAMED_PROPS: true,
});

/**
 * A GFM task-list box as a glyph, never an `<input>`: the sanitizers forbid form controls (`MARKDOWN_PURIFY_CONFIG`;
 * the agent renderer's allowlist never had them). Shaped as marked's `checkbox` renderer, so a `Marked` instance
 * takes it as `renderer: { checkbox: render_task_box }`.
 */
export function render_task_box({ checked }: Pick<Tokens.Checkbox, 'checked'>): string {
    return `<span class="vc-task-box" aria-hidden="true">${checked ? '☑' : '☐'}</span> `;
}
