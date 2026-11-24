<template>
    <section class="fui-panel st-card st-directories">
        <LaunchDirectorySettings :view="view" :disabled="bridge.down.value !== null" @save="(value) => feed.save(value)" />
        <p class="st-card__about">
            These belong to the bridge, so every device that opens this rack sees the same directories. Worktree and LEAD launches stay
            tied to the directory the bridge was started in.
        </p>
    </section>
</template>

<script setup lang="ts">
/**
 * DIRECTORIES: where a new agent starts. The form is the rack's own
 * (`agent-rack/LaunchDirectorySettings.vue`), fed by the rack's own client through
 * `directories-feed.ts` — see the note there.
 *
 * The first reload subscribes, which is the feed's first read. Every later one (RETRY, the
 * browser tab back in front) asks the feed to read again; the client under it drops a read that
 * would only repeat one already out.
 */
import { onBeforeUnmount, ref } from 'vue';

import LaunchDirectorySettings from '../agent-rack/LaunchDirectorySettings.vue';
import type { LaunchDirectoriesView } from '../agent-rack/launch-directories-client.ts';
import { create_directories_feed } from './directories-feed.ts';
import { use_reload } from './page-bridge.ts';

const view = ref<LaunchDirectoriesView | undefined>();
const feed = create_directories_feed((down) => bridge.note(down ?? { ok: true, data: null }));
let stop: (() => void) | null = null;

const bridge = use_reload(() => {
    if (stop) feed.refresh();
    else
        stop = feed.subscribe((next) => {
            view.value = next;
        });
});

onBeforeUnmount(() => stop?.());
</script>
