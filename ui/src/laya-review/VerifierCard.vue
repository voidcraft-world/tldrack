<template>
    <details class="fui-panel verifier" @toggle="toggle">
        <summary>VERIFIER CARD</summary>
        <p v-if="card">Publish to <a :href="`https://github.com/${card.repository}`" target="_blank" rel="noopener">{{ card.repository }}</a> as a pull request.</p>
        <p v-if="message" role="status">{{ message }}</p>
        <template v-if="card">
            <span class="fui-tag">{{ card.pairs }} κ PAIRS · {{ card.at.slice(0, 10) }}</span>
            <details v-for="(text, name) in card.files" :key="name" :open="name === 'VERIFIER.md'">
                <summary>{{ name }}</summary>
                <pre>{{ text }}</pre>
            </details>
            <button class="board-btn" type="button" :disabled="busy || !card.ready" :title="card.ready ? 'Open a public PR containing exactly the files shown above' : 'Needs at least 20 measurable sample kappa pairs'" @click="publish">
                {{ busy ? 'PUBLISHING…' : armed ? 'PUBLISH TO TLDRACK?' : 'PUBLISH' }}
            </button>
            <button class="board-btn" type="button" :disabled="busy" @click="load">REFRESH</button>
            <a v-if="url" :href="url" target="_blank" rel="noopener">OPEN PR ↗</a>
        </template>
    </details>
</template>
<script setup lang="ts">
import { ref } from 'vue';
import type { LayaVerifierCard } from '@tldrack/schemas/laya-review.ts';
import { load_laya_verifier, publish_laya_verifier, fail_text } from '../decide/laya-review-client.ts';
const card = ref<LayaVerifierCard | null>(null);
const busy = ref(false);
const armed = ref(false);
const message = ref('');
const url = ref('');
async function load() {
    url.value = '';
    armed.value = false;
    const result = await load_laya_verifier();
    if (result.ok) {
        card.value = result.data.card;
        message.value = card.value ? (card.value.ready ? '' : 'Held until 20 measurable sample κ pairs.') : 'No card yet. Run laya-lab card to build it.';
    } else { card.value = null; message.value = fail_text(result); }
}
function toggle(event: Event) {
    if (event.target !== event.currentTarget) return;
    if ((event.target as HTMLDetailsElement).open) void load();
    else armed.value = false;
}
async function publish() {
    if (!card.value?.ready || busy.value) return;
    if (!armed.value) { armed.value = true; return; }
    busy.value = true;
    const result = await publish_laya_verifier(card.value.digest);
    busy.value = false;
    armed.value = false;
    if (result.ok) { url.value = result.data.url; message.value = result.data.message; }
    else message.value = fail_text(result);
}
</script>
<style scoped>
.verifier { margin: .5rem 0; padding: .6rem; }
.verifier p { font-size: .8rem; }
.verifier summary { cursor: pointer; font-size: .78rem; }
.verifier pre { white-space: pre-wrap; overflow-wrap: anywhere; font-size: .8rem; max-height: 28rem; overflow-y: auto; }
</style>
