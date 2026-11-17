// `review.html`: Laya review. The page talks only to the bridge, whose guards are the gate.
// Import order: owned by storage/migrate-keys.ts — see the note there.
import '../storage/migrate-keys.ts';
import '@fontsource/roboto/latin-400.css';
import '../styles/boot.ts';
// The page skin and the page's shared pieces, once and globally: each stylesheet's header owns why.
import '../styles/native-view-board.css';
import './laya-review.css';

import { createApp } from 'vue';
import LayaReviewDeck from './LayaReviewDeck.vue';
import { page_bridge_origin, parse_host_list } from '../agent-rack/rack-remote.ts';
import { set_bridge_origin } from '../config/bridge-origin.ts';
import { ALLOWED_REMOTE_HOSTS } from '../global/environment.ts';

// Before mount: every client reads it (`config/bridge-origin.ts`).
const bridge = page_bridge_origin(window.location, parse_host_list(ALLOWED_REMOTE_HOSTS));
if (bridge !== null) set_bridge_origin(bridge);

createApp(LayaReviewDeck).mount('#app');
