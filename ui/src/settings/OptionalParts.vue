<template>
    <section class="fui-panel st-card" aria-labelledby="st-optional-title">
        <div class="st-card__head">
            <h2 id="st-optional-title" class="fui-techtext st-card__title">OPTIONAL PARTS</h2>
        </div>
        <p class="st-card__about">Three parts the bridge keeps a switch for by itself. Each change lands at once.</p>
        <ul class="st-rows">
            <li class="st-row">
                <div class="st-row__body">
                    <span class="st-row__title">Next step suggester</span>
                    <p class="st-row__text">
                        When a turn ends, names the one rack control you would likely press next, with its reason. It never presses anything.
                        Needs Ollama running on this machine<template v-if="next_step.data?.model"> (model <code>{{ next_step.data.model }}</code>)</template>:
                        every finished turn is one local model call.
                    </p>
                    <p v-if="next_step.data" class="st-row__text">{{ NEXT_STEP_MODE_NOTES[next_step.data.mode] }}</p>
                    <p v-if="next_step.problem" class="st-row__note" role="alert">{{ next_step.problem }}</p>
                </div>
                <SettingChoice
                    v-if="next_step.data"
                    name="st-next-step"
                    label="Next step suggester"
                    :options="MODE_OPTIONS"
                    :value="next_step.data.mode"
                    :disabled="next_step.saving"
                    @set="set_mode"
                />
            </li>
            <li class="st-row">
                <div class="st-row__body">
                    <span class="st-row__title">Phone pushes</span>
                    <p id="st-notify-text" class="st-row__text">A push to your phone, through ntfy, when an agent is blocked on you.</p>
                    <p v-if="notify.data && !notify.data.configured" class="st-row__note">
                        No topic is set, so there is nowhere to push to. Make one in a terminal: <code>{{ NOTIFY_INIT_COMMAND }}</code>.
                        The topic is the channel’s only secret, so the bridge takes none from a page.
                    </p>
                    <p v-if="notify.problem" class="st-row__note" role="alert">{{ notify.problem }}</p>
                </div>
                <SettingSwitch
                    v-if="notify.data"
                    :on="notify.data.configured && notify.data.enabled"
                    label="Phone pushes"
                    describedby="st-notify-text"
                    :disabled="!notify.data.configured || notify.saving"
                    @set="set_pushes"
                />
            </li>
            <li class="st-row">
                <div class="st-row__body">
                    <span class="st-row__title">Rack metrics</span>
                    <p id="st-metrics-text" class="st-row__text">
                        Records which rows you select and how long a pane is in front, in a database on this machine, so a change to the rack
                        can be measured. Names and counts only: never a prompt, a path or terminal text. Off pauses the recording and keeps what is there.
                    </p>
                    <p v-if="metrics.problem" class="st-row__note" role="alert">{{ metrics.problem }}</p>
                </div>
                <SettingSwitch
                    v-if="metrics.data"
                    :on="metrics.data.enabled"
                    label="Rack metrics"
                    describedby="st-metrics-text"
                    :disabled="metrics.saving"
                    @set="set_metrics"
                />
            </li>
        </ul>
    </section>
</template>

<script setup lang="ts">
/**
 * FEATURES › OPTIONAL PARTS: the next step suggester, the phone pushes and the rack metrics. The
 * routes and what each reply carries: owned by `optional-parts-client.ts` — see the note there.
 *
 * Each row stands alone: a part this bridge lacks says so on its own row and leaves the other
 * two working. A switch moves when the bridge answers, never before.
 */
import { reactive } from 'vue';

import SettingChoice from './SettingChoice.vue';
import SettingSwitch from './SettingSwitch.vue';
import { down_after, type BridgeResult } from './bridge-call.ts';
import {
    NEXT_STEP_MODES,
    NEXT_STEP_MODE_NOTES,
    NOTIFY_INIT_COMMAND,
    fetch_next_step,
    fetch_notify,
    fetch_rack_metrics,
    set_next_step_mode,
    set_notify,
    set_rack_metrics,
    type NextStepPart,
    type NextStepSwitch,
    type NotifyPart,
    type RackMetricsPart,
} from './optional-parts-client.ts';
import { use_reload } from './page-bridge.ts';

interface Part<T> {
    data: T | null;
    /** Why the row has no switch, or why the last press did not take. Never "the bridge is down": the banner says that. */
    problem: string | null;
    saving: boolean;
}
const empty = <T,>(): Part<T> => ({ data: null, problem: null, saving: false });

const next_step = reactive(empty<NextStepPart>());
const notify = reactive(empty<NotifyPart>());
const metrics = reactive(empty<RackMetricsPart>());

const MODE_OPTIONS = NEXT_STEP_MODES.map((mode) => ({ value: mode, label: mode.toUpperCase(), title: NEXT_STEP_MODE_NOTES[mode] }));

/** One result into its row. The last good answer stays on screen under a failed re-read. */
function take<T>(part: Part<T>, result: BridgeResult<T>): void {
    if (result.ok) {
        part.data = result.data;
        part.problem = null;
    } else if (result.kind === 'missing') {
        part.problem = 'The running bridge does not have this part.';
    } else {
        part.problem = result.kind === 'unreachable' ? null : result.error;
    }
}

let generation = 0;

async function load(): Promise<void> {
    const mine = ++generation;
    const [step, pushes, recorded] = await Promise.all([fetch_next_step(), fetch_notify(), fetch_rack_metrics()]);
    if (mine !== generation) return;
    // One slow route is not a down bridge: the page hears "down" only when none of the three answered.
    const results: Array<BridgeResult<unknown>> = [step, pushes, recorded];
    bridge.note(results.find((result) => down_after(result) === null) ?? step);
    take(next_step, step);
    take(notify, pushes);
    take(metrics, recorded);
}

const bridge = use_reload(() => void load());

/** One press: hold the row's control, write, and draw what the bridge answered. */
async function press<T>(part: Part<T>, write: () => Promise<BridgeResult<T>>): Promise<void> {
    part.saving = true;
    generation += 1; // a read already out describes the part before this press
    const result = bridge.note(await write());
    part.saving = false;
    take(part, result);
}

function set_mode(mode: NextStepSwitch): void {
    // The reply names the mode alone, so the model shown beside it is kept from the last read.
    void press(next_step, async () => {
        const result = await set_next_step_mode(mode);
        return result.ok ? { ok: true, data: { ...next_step.data, mode: result.data.mode } } : result;
    });
}

function set_pushes(enabled: boolean): void {
    void press(notify, () => set_notify(enabled));
}

function set_metrics(enabled: boolean): void {
    void press(metrics, () => set_rack_metrics(enabled));
}
</script>
