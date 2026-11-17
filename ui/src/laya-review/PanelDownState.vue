<!--
    The "there is nothing to show yet, and that is normal" callout: a mono head, the
    WHY in one line, and the remedy under it.

    Slate, not amber, on purpose: a page that asks the local bridge a question can
    legitimately have no answer yet (nothing judged, nothing loaded, the bridge not
    connected), so this reads as an instruction and not as an error. Every empty and
    down state of the review page draws it.
-->
<template>
    <div class="panel-down">
        <div class="fui-techtext panel-down__head">{{ head }}</div>
        <div v-if="$slots.why" class="panel-down__why"><slot name="why" /></div>
        <div v-if="$slots.how" class="panel-down__how"><slot name="how" /></div>
    </div>
</template>

<script setup lang="ts">
defineProps<{
    /** Short uppercase state, e.g. `NO OUTPUT FOLDER`. */
    head: string;
}>();
</script>

<style scoped>
.panel-down {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 14px 16px;
    border: 1px solid rgba(var(--fui-slate-rgb), 0.4);
    border-radius: 8px;
    background: rgba(var(--fui-slate-rgb), 0.07);
}
.panel-down__head {
    font-size: 0.7rem;
    color: var(--fui-slate);
}
.panel-down__why {
    font-size: 0.82rem;
}
.panel-down__how {
    font-size: 0.78rem;
    opacity: 0.75;
}
</style>
