/**
 * The operator's MOTION switch (`config/rack-prefs.ts` `motion`) applied to the motion gate
 * (`config/fui-motion-flags.ts` `enabled`), on every page, as it boots and whenever the switch
 * changes in another tab.
 *
 * WHY HERE. `enabled` had no control anywhere, and the stylesheet's whole motion story is the
 * one class the gate writes, so the switch has to reach the gate on every page that takes the
 * stylesheet. `boot.ts` is the one module every page entry imports, which is what makes this a
 * property of the pages rather than a line each entry has to remember.
 *
 * Only `enabled` is written. WORK, the running scanner, keeps moving with the switch off: owned
 * by config/fui-motion-flags.ts — see the note there.
 *
 * A HOST'S OWN PREFERENCE. A host that embeds the rack may write `enabled` itself after mount.
 * Its write stands until the stored switch next changes, and a page that has stored nothing
 * boots to `true`, the gate's own default.
 */
import { FUI_MOTION_FLAGS } from '../config/fui-motion-flags.ts';
import { load_rack_prefs, watch_rack_prefs, type RackPrefs } from '../config/rack-prefs.ts';

/** Set the gate from storage now and on every later change. Returns the stop function. */
export function follow_motion_pref(
    flags: { enabled: boolean } = FUI_MOTION_FLAGS,
    load: () => RackPrefs = load_rack_prefs,
    watch: (on_change: (prefs: RackPrefs) => void) => () => void = watch_rack_prefs,
): () => void {
    flags.enabled = load().motion;
    return watch((prefs) => {
        flags.enabled = prefs.motion;
    });
}

// For the page's lifetime, like the gate's own OS listener: there is no moment a page stops
// owing the operator their switch.
follow_motion_pref();
