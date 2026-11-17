// `evals.html`: the eval results, as one static page. It reads no bridge and no network: the
// results file is in the bundle (`results-source.ts`).
import './evals.css';

import { createApp } from 'vue';
import EvalsPage from './EvalsPage.vue';
import { load_results } from './results-source.ts';

// `?theme=light|dark` stamps a theme over the OS setting: how the README figures are taken in
// both. Without it the page follows the OS.
const theme = new URLSearchParams(window.location.search).get('theme');
if (theme === 'light' || theme === 'dark') document.documentElement.dataset.theme = theme;

createApp(EvalsPage, load_results()).mount('#app');
