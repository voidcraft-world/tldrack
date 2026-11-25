<template>
    <label class="st-switch">
        <input
            type="checkbox"
            role="switch"
            :checked="on"
            :disabled="disabled"
            :aria-label="label"
            :aria-describedby="describedby"
            @change="on_change"
        />
        <span class="st-switch__track" aria-hidden="true"></span>
        <span class="st-switch__word" aria-hidden="true">{{ on ? 'ON' : 'OFF' }}</span>
    </label>
</template>

<script setup lang="ts">
/**
 * One on/off switch: a real checkbox with the switch role, drawn as a track.
 *
 * CONTROLLED. The box shows `on` and nothing else. A press asks (`set`) and the box is put back
 * to `on` at once, so it moves only when the owner's state does: a write the bridge refused
 * leaves the switch where the truth is, with no second code path to undo an optimistic flip.
 */
const props = defineProps<{
    on: boolean;
    /** The switch's accessible name: what it turns on. */
    label: string;
    disabled?: boolean;
    /** The id of the text that explains it. */
    describedby?: string;
}>();
const emit = defineEmits<{ set: [on: boolean] }>();

function on_change(event: Event): void {
    const box = event.target as HTMLInputElement;
    const wanted = box.checked;
    box.checked = props.on;
    emit('set', wanted);
}
</script>
