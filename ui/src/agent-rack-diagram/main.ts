// `diagram.html`: the rack's ⌗ HOW window. Static: it reads no bridge.
import '@fontsource/roboto/latin-400.css';
import '../styles/boot.ts';

import { createApp } from 'vue';
import AgentRackDiagram from './AgentRackDiagram.vue';

createApp(AgentRackDiagram).mount('#app');
