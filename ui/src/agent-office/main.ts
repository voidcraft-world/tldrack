// `office.html`: the rack's OFFICE window. It reads no bridge: the rack arrives from a rack
// page over its own channel (engine/terminal/agent-office-link.ts).
// Import order: owned by storage/migrate-keys.ts — see the note there.
import '../storage/migrate-keys.ts';
import '@fontsource/roboto/latin-400.css';
import '../styles/boot.ts';

import { createApp } from 'vue';
import AgentOfficeWindow from './AgentOfficeWindow.vue';

createApp(AgentOfficeWindow).mount('#app');
