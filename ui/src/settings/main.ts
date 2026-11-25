// `settings.html`: the rack's settings, as a page of its own. It talks to one bridge, whose guards
// are the gate: the one that served it, or the one this device's rack is pointed at
// (`rack-remote.ts` `page_bridge_origin`).
// Import order: owned by storage/migrate-keys.ts — see the note there.
import '../storage/migrate-keys.ts';
import '@fontsource/roboto/latin-400.css';
import '../styles/boot.ts';
// The review page's skin, then this page's own pieces on top of it: settings.css owns why.
import '../styles/native-view-board.css';
import '../laya-review/laya-review.css';
import './settings.css';

import { createApp } from 'vue';
import SettingsPage from './SettingsPage.vue';
import { parse_host_list, resolve_page_bridge } from '../agent-rack/rack-remote.ts';
import { set_bridge_origin } from '../config/bridge-origin.ts';
import { ALLOWED_REMOTE_HOSTS } from '../global/environment.ts';

// Before mount: every client reads it (`config/bridge-origin.ts`).
const bridge = resolve_page_bridge(window.location, parse_host_list(ALLOWED_REMOTE_HOSTS));
if (bridge !== null) set_bridge_origin(bridge);

createApp(SettingsPage).mount('#app');
