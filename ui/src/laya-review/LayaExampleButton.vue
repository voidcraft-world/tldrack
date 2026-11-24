<template>
    <div class="lr-example" @click.stop>
        <button
            type="button"
            class="board-btn"
            :class="{ 'is-on': marked }"
            :aria-pressed="marked"
            :disabled="disabled || saving"
            title="Save this turn as a great example for future synthetic training data. Press again to remove."
            @click="toggle"
        >{{ marked ? '★' : '☆' }} GREAT EXAMPLE</button>
        <span v-if="message" role="status">{{ message }}</span>
    </div>
</template>

<script setup lang="ts">
import { ref } from 'vue';
import { fail_text, post_review_example } from '../decide/laya-review-client.ts';

const props = defineProps<{ surface: string; item_key: string; marked: boolean; disabled?: boolean }>();
const emit = defineEmits<{ saved: [marked: boolean] }>();
const saving = ref(false);
const message = ref('');

async function toggle(): Promise<void> {
    if (saving.value || props.disabled) return;
    saving.value = true;
    message.value = '';
    try {
        const got = await post_review_example({ surface: props.surface, key: props.item_key, great_example: !props.marked });
        if (got.ok) {
            emit('saved', got.data.great_example);
            message.value = got.data.message;
        } else message.value = fail_text(got);
    } finally {
        saving.value = false;
    }
}
</script>

<style scoped>
.lr-example {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    grid-column: 1 / -1;
    flex-basis: 100%;
}
.lr-example span { font-size: 0.8rem; color: var(--lr-ink-2); }
</style>
