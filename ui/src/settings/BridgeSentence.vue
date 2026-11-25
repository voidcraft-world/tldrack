<template>
    <template v-for="(span, index) in spans" :key="index"><code v-if="span.code">{{ span.text }}</code><template v-else>{{ span.text }}</template></template>
</template>

<script setup lang="ts">
/**
 * One sentence in the bridge's own words, with the names it marked in backticks drawn as code.
 * Text only ever: a span is interpolated, never parsed as markup. The marks: owned by
 * `update/update-status.ts` `code_spans` — see the note there.
 */
import { computed } from 'vue';

import { code_spans } from '../update/update-status.ts';

const props = defineProps<{ text: string }>();
const spans = computed(() => code_spans(props.text));
</script>
