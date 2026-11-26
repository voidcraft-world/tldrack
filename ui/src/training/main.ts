// `training.html`: Laya training. The page talks only to the bridge, whose guards are the gate; the eval
// results it draws the subscription models from are in the bundle (`evals/results-source.ts`).
// Import order: owned by storage/migrate-keys.ts — see the note there.
import '../storage/migrate-keys.ts';
import '@fontsource/roboto/latin-400.css';
import '../styles/boot.ts';
// The page skin, once and globally: owned by styles/native-view-board.css — see the note there.
import '../styles/native-view-board.css';

import { createApp } from 'vue';
import TrainingPage from './TrainingPage.vue';
import { parse_host_list, resolve_page_bridge } from '../agent-rack/rack-remote.ts';
import { set_bridge_origin } from '../config/bridge-origin.ts';
import { load_results } from '../evals/results-source.ts';
import { ALLOWED_REMOTE_HOSTS } from '../global/environment.ts';

// Before mount: every client reads it (`config/bridge-origin.ts`).
const bridge = resolve_page_bridge(window.location, parse_host_list(ALLOWED_REMOTE_HOSTS));
if (bridge !== null) set_bridge_origin(bridge);

createApp(TrainingPage, { bench: load_results().raw }).mount('#app');
