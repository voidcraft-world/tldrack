<template>
    <fieldset class="st-choice" :disabled="disabled">
        <legend class="st-sr">{{ label }}</legend>
        <label v-for="option in options" :key="option.value" :title="option.title">
            <input type="radio" :name="name" :value="option.value" :checked="option.value === value" @change="on_change($event, option.value)" />
            <span>{{ option.label }}</span>
        </label>
    </fieldset>
</template>

<script setup lang="ts" generic="T extends string">
/**
 * One choice among a few named values: real radio buttons in a fieldset, drawn as a segmented
 * control. Controlled the way `SettingSwitch.vue` is — see the note there.
 */
const props = defineProps<{
    /** The radio group's name: unique on the page. */
    name: string;
    /** The group's accessible name. */
    label: string;
    options: ReadonlyArray<{ value: T; label: string; title?: string }>;
    value: T;
    disabled?: boolean;
}>();
const emit = defineEmits<{ set: [value: T] }>();

function on_change(event: Event, wanted: T): void {
    const picked = event.target as HTMLInputElement;
    // Back to what the owner holds: the radios of one name move together, so restoring the
    // current one un-checks the pressed one.
    const group = picked.closest('fieldset')?.querySelectorAll<HTMLInputElement>('input[type="radio"]') ?? [];
    for (const radio of group) radio.checked = radio.value === props.value;
    emit('set', wanted);
}
</script>
