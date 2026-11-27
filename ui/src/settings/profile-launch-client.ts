import { z } from 'zod';

import { bridge_read, bridge_write, type BridgeResult } from './bridge-call.ts';

const profile_launch_schema = z.object({
    id: z.string().max(32),
    label: z.string().max(10),
    kind: z.enum(['claude', 'codex']),
    profile_model_label: z.string().max(64).nullable(),
    model: z.string().max(128).nullable(),
    reasoning_effort: z.string().max(16).nullable(),
});
const launch_settings_schema = z.object({
    profiles: z.array(profile_launch_schema),
    reasoning_efforts: z.array(z.string().min(1).max(16)),
    error: z.string().nullable(),
});
export type ProfileLaunch = z.infer<typeof profile_launch_schema>;
export type LaunchSettings = z.infer<typeof launch_settings_schema>;

export function fetch_launch_settings(): Promise<BridgeResult<LaunchSettings>> {
    return bridge_read('/profile-launch-settings', launch_settings_schema, 'launch defaults');
}

export function save_launch_settings(profile: string, model: string | null, reasoning_effort: string | null): Promise<BridgeResult<LaunchSettings>> {
    return bridge_write('/profile-launch-settings', { profile, model, reasoning_effort }, launch_settings_schema, 'saved launch defaults');
}
