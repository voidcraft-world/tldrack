<script setup lang="ts">
/**
 * MISSIONS, under the rack's session list: agents assigned to one task. Everything here is one
 * `POST /missions`, and every member hears the change through its hooks, so nothing is typed into
 * a pane. Owner of what a mission is: `bridge/tldrack/missions.py`.
 */
import { computed, nextTick, ref } from 'vue';
import { CANNOT_HEAR, type Mission, type MissionMember, type MissionRequest } from './missions-client.ts';

const props = defineProps<{
    missions: readonly Mission[];
    /** Live rack sessions, for the add list and for labels. */
    sessions: readonly { session: string; label: string }[];
    /** A new mission starts with this session on it, and the bar says so. */
    focused: string | null;
    disabled?: boolean;
}>();
const emit = defineEmits<{ act: [request: MissionRequest] }>();

const creating = ref(false);
const title_input = ref<HTMLInputElement | null>(null);
const closing = ref<string | null>(null);
/** The role box being typed in. Vue re-applies `:value` on every render and the payload arrives
 *  every few seconds, so without this a poll wipes what is being typed. */
const role_drafts = ref<Record<string, string>>({});

const labels = computed(() => new Map(props.sessions.map((row) => [row.session, row.label])));
const unassigned = computed(() => {
    const taken = new Set(props.missions.flatMap((mission) => mission.members.map((member) => member.session)));
    return props.sessions.filter((row) => !taken.has(row.session));
});

function label(session: string): string {
    return labels.value.get(session) ?? session;
}

async function start_create(): Promise<void> {
    creating.value = true;
    await nextTick();
    title_input.value?.focus();
}

function create(event: Event): void {
    const input = (event.target as HTMLFormElement).elements.namedItem('title') as HTMLInputElement;
    const title = input.value.trim();
    creating.value = false;
    if (title) emit('act', { op: 'create', title, sessions: props.focused ? [props.focused] : [] });
}

function add_member(mission: Mission, event: Event): void {
    const select = event.target as HTMLSelectElement;
    if (select.value) emit('act', { op: 'assign', session: select.value, mission: mission.id });
    select.value = '';
}

function set_role(member: MissionMember, event: Event): void {
    const role = (event.target as HTMLInputElement).value.trim();
    if (role !== member.role) emit('act', { op: 'role', session: member.session, role });
}

function member_title(member: MissionMember): string {
    return member.hears === false ? `${member.session}. ${CANNOT_HEAR}` : member.session;
}

function send_note(mission: Mission, event: Event): void {
    const form = event.target as HTMLFormElement;
    const input = form.elements.namedItem('note') as HTMLInputElement;
    const text = input.value.trim();
    if (!text) return;
    emit('act', { op: 'note', mission: mission.id, text });
    input.value = '';
}

function close_mission(mission: Mission): void {
    if (closing.value !== mission.id) {
        closing.value = mission.id;
        return;
    }
    closing.value = null;
    emit('act', { op: 'close', mission: mission.id });
}
</script>

<template>
    <section class="missions" @click.stop @contextmenu.stop>
        <div class="missions__head">
            <span v-if="missions.length">MISSIONS · {{ missions.length }}</span>
            <button v-if="!creating" type="button" class="missions__new" :disabled="disabled"
                :title="focused ? `Start a mission with ${label(focused)} on it` : 'Start a mission'"
                aria-label="Start a mission" @click="start_create">+</button>
        </div>
        <form v-if="creating" class="missions__form" @submit.prevent="create">
            <input ref="title_input" name="title" class="missions__input" maxlength="120" autocomplete="off"
                :placeholder="focused ? `What is the task? Starts with ${label(focused)}` : 'What is the task?'"
                aria-label="Mission title" @keydown.esc="creating = false" @blur="creating = false" />
        </form>

        <details v-for="mission in missions" :key="mission.id" class="mission">
            <summary class="fui-techtext">
                <span class="mission__title" :title="mission.brief || mission.title">{{ mission.title }}</span>
                <span class="mission__count" :title="`${mission.members.length} agents, ${mission.note_count} notes`">
                    {{ mission.members.length }} · {{ mission.note_count }}
                </span>
            </summary>
            <ul class="mission__members">
                <li v-for="member in mission.members" :key="member.session" class="mission__member"
                    :class="{ 'mission__member--deaf': member.hears === false }">
                    <span class="mission__name" :title="member_title(member)" :aria-label="member_title(member)">{{ label(member.session) }}</span>
                    <input class="missions__input" maxlength="200" autocomplete="off" placeholder="what it owns"
                        :value="role_drafts[member.session] ?? member.role" :disabled="disabled"
                        :aria-label="`What ${label(member.session)} owns`"
                        @focus="role_drafts[member.session] = member.role"
                        @input="role_drafts[member.session] = ($event.target as HTMLInputElement).value"
                        @change="set_role(member, $event)"
                        @blur="delete role_drafts[member.session]"
                        @keydown.enter="($event.target as HTMLInputElement).blur()" />
                    <button type="button" class="missions__remove" :disabled="disabled"
                        :title="`Take ${label(member.session)} off this mission`"
                        @click="emit('act', { op: 'assign', session: member.session, mission: null })">✕</button>
                    <span v-if="member.touching.length" class="mission__touching">{{ member.touching.join(' · ') }}</span>
                </li>
            </ul>
            <select v-if="unassigned.length" class="missions__input" :disabled="disabled"
                aria-label="Add an agent to this mission" @change="add_member(mission, $event)">
                <option value="">+ agent</option>
                <option v-for="row in unassigned" :key="row.session" :value="row.session">{{ row.label }}</option>
            </select>
            <ol v-if="mission.notes.length" class="mission__notes">
                <li v-for="note in mission.notes" :key="note.seq">
                    <span class="mission__author">{{ label(note.author) }}</span> {{ note.text }}
                </li>
            </ol>
            <form class="missions__form" @submit.prevent="send_note(mission, $event)">
                <input name="note" class="missions__input" maxlength="1000" autocomplete="off"
                    placeholder="Note for every agent on this" aria-label="Note for every agent on this mission"
                    title="Agents read this as information. To direct one, prompt it." :disabled="disabled" />
            </form>
            <button type="button" class="fui-btn mission__close" :disabled="disabled" @click="close_mission(mission)"
                @blur="closing = null">{{ closing === mission.id ? 'Close mission?' : 'Close' }}</button>
        </details>
    </section>
</template>

<style scoped>
.missions { flex: 0 0 auto; display: flex; flex-direction: column; gap: 4px; }
/* `.missions__head`: owned by AgentRackComponent.vue — see the DEVICES head there. */
.missions__new, .missions__remove {
    padding: 0 4px;
    background: transparent;
    border: 0;
    color: inherit;
    font: inherit;
    cursor: pointer;
    min-width: 28px;
    min-height: 28px;
}
.missions__new { margin-left: auto; }
.missions__remove { color: rgba(244, 247, 255, 0.5); }
.missions__form { padding: 0 6px; }
.missions__input {
    width: 100%;
    min-width: 0;
    padding: 2px 4px;
    background: rgba(244, 247, 255, 0.05);
    border: 1px solid rgba(244, 247, 255, 0.14);
    color: inherit;
    font: inherit;
    font-size: 12.5px;
}
.mission { padding: 0 6px; font-size: 12.5px; }
.mission summary { display: flex; justify-content: space-between; gap: 8px; cursor: pointer; }
.mission__title { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mission__count { flex: 0 0 auto; opacity: 0.6; font-variant-numeric: tabular-nums; }
.mission__members, .mission__notes { list-style: none; margin: 4px 0; padding: 0; display: flex; flex-direction: column; gap: 3px; }
.mission__member { display: grid; grid-template-columns: minmax(0, 0.8fr) minmax(0, 1fr) auto; gap: 4px; align-items: center; }
.mission__name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mission__member--deaf .mission__name { opacity: 0.45; }
.mission__touching { grid-column: 1 / -1; font-size: 11px; opacity: 0.55; overflow-wrap: anywhere; }
.mission__notes { max-height: 132px; overflow-y: auto; }
.mission__author { color: var(--color-accent-green, #93ffd4); }
.mission__close { margin: 4px 0; }
</style>
