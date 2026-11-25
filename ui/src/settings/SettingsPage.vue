<template>
    <div class="nv-board lr st">
        <header class="nv-head fui-panel st-head">
            <span class="fui-corner tl"></span>
            <span class="fui-corner br"></span>
            <h1 class="fui-techtext st-title">SETTINGS</h1>
            <span class="fui-tag st-origin" :title="`This page reads and writes the bridge at ${origin}`">{{ origin_label }}</span>
            <span class="lr-grow"></span>
            <nav class="st-links" aria-label="Other pages">
                <a v-for="link in links" :key="link.href" class="board-btn" :href="link.href" :title="link.title">
                    <span class="fui-corner tl"></span><span class="fui-corner br"></span>{{ link.label }}
                </a>
            </nav>
        </header>

        <div class="st-tabs" role="tablist" aria-label="Settings">
            <button
                v-for="entry in SETTINGS_TABS"
                :id="tab_id(entry.id)"
                :key="entry.id"
                type="button"
                role="tab"
                class="board-btn"
                :class="{ 'is-on': tab === entry.id }"
                :aria-selected="tab === entry.id"
                :aria-controls="tab === entry.id ? PANEL_ID : undefined"
                :tabindex="tab === entry.id ? 0 : -1"
                :title="entry.title"
                @click="select(entry.id)"
                @keydown="on_tab_key"
            >
                <span class="fui-corner tl"></span><span class="fui-corner br"></span>{{ entry.label }}
            </button>
        </div>

        <BridgeDown v-if="bridge.down.value" :failure="bridge.down.value" @retry="bridge.retry()" />

        <main :id="PANEL_ID" class="st-panel" role="tabpanel" :aria-labelledby="tab_id(tab)">
            <SetupTab v-if="tab === 'setup'" />
            <FeaturesTab v-else-if="tab === 'features'" />
            <AppearanceTab v-else-if="tab === 'appearance'" />
            <DirectoriesTab v-else />
        </main>
    </div>
</template>

<script setup lang="ts">
/**
 * `settings.html`: the rack's settings, as a page of its own. Four tabs:
 *
 *   SETUP        what this machine has (`GET /doctor`), the hooks and Laya still to install
 *   FEATURES     what this browser draws, and what the bridge runs
 *   APPEARANCE   the terminal's font, renderer and theme
 *   DIRECTORIES  where a new agent starts
 *
 * The page talks to the bridge that served it (`main.ts` sets the origin). Only the open tab is
 * mounted, so only the open tab reads the bridge. The tab is named by the URL hash
 * (`settings-tabs.ts`), written with `replaceState`: switching tabs is not a history entry, and
 * a link to `#features` opens there.
 */
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue';

import { bridge_origin } from '../config/bridge-origin.ts';
import { unlock_local_access } from '../engine/terminal/local-network-gate.ts';
import AppearanceTab from './AppearanceTab.vue';
import BridgeDown from './BridgeDown.vue';
import DirectoriesTab from './DirectoriesTab.vue';
import FeaturesTab from './FeaturesTab.vue';
import SetupTab from './SetupTab.vue';
import { provide_page_bridge } from './page-bridge.ts';
import { SETTINGS_TABS, page_links, tab_for_key, tab_from_hash, tab_hash, type SettingsTabId } from './settings-tabs.ts';

const PANEL_ID = 'st-panel';
const tab_id = (id: SettingsTabId): string => `st-tab-${id}`;

const bridge = provide_page_bridge();
const origin = bridge_origin();
const origin_label = origin.replace(/^https?:\/\//, '');
const links = page_links(window.location.href);

const tab = ref<SettingsTabId>(tab_from_hash(window.location.hash));

function select(id: SettingsTabId): void {
    tab.value = id;
    try {
        window.history.replaceState(null, '', tab_hash(id));
    } catch {
        // A sandboxed frame may refuse history writes: the tab still switches.
    }
}

function on_tab_key(event: KeyboardEvent): void {
    const next = tab_for_key(tab.value, event.key);
    if (next === null) return;
    event.preventDefault();
    select(next);
    void nextTick(() => document.getElementById(tab_id(next))?.focus());
}

/** A hash typed into the address bar, or a link on this page (`#features`), is a tab switch too. */
function on_hash_change(): void {
    tab.value = tab_from_hash(window.location.hash);
}

onMounted(() => {
    // This page IS a local-tools surface, so it opens the gate itself: owned by
    // laya-review/LayaReviewDeck.vue `onMounted` — see the note there.
    unlock_local_access();
    window.addEventListener('hashchange', on_hash_change);
});
onBeforeUnmount(() => window.removeEventListener('hashchange', on_hash_change));
</script>
