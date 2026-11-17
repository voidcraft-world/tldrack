/**
 * FNV-1a over a string → 32-bit unsigned. The ONE owner of this hash for the
 * modules that have to agree on the same number for the same input.
 *
 * Import-free on purpose: its user, `engine/terminal/agent-zone-logic.ts`, runs
 * under bare `node --test`.
 *
 * `laya-review/thread-model.ts` carries a second copy for a thread's mark. The
 * two hash different things and do not have to agree.
 */
export function fnv1a(text: string): number {
    let hash = 0x811c9dc5;
    for (let i = 0; i < text.length; i++) {
        hash ^= text.charCodeAt(i);
        hash = Math.imul(hash, 0x01000193) >>> 0;
    }
    return hash;
}
