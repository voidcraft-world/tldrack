/** Reuse DEVICES' cached process sample. The office never starts another poller. */
import type { DevicesState } from './device-readout.ts';
export interface OfficeResource { session: string; rss: number; cpu_pct: number | null }
export interface OfficeResources { sessions: OfficeResource[]; stale: boolean }
function amount(value: unknown): number | null {
    return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
}
export function parse_office_resources(raw: unknown): OfficeResources | undefined {
    if (!raw || typeof raw !== 'object') return undefined;
    const data = raw as Record<string, unknown>;
    if (!Array.isArray(data.sessions) || data.sessions.length > 500) return undefined;
    return { stale: data.stale === true, sessions: data.sessions.flatMap(value => {
        if (!value || typeof value !== 'object') return [];
        const row = value as Record<string, unknown>;
        const rss = amount(row.rss);
        return typeof row.session === 'string' && row.session.length <= 400 && rss !== null
            ? [{ session: row.session, rss, cpu_pct: amount(row.cpu_pct) }] : [];
    }) };
}
export function office_resources(devices: DevicesState, open: boolean): OfficeResources | undefined {
    const local = devices.devices.find(device => device.local);
    if (!local?.session_memory) return undefined;
    return { sessions: local.session_memory.sessions.map(row => ({ session: row.session, rss: row.rss, cpu_pct: row.cpu_pct ?? null })),
        stale: !open || local.state !== 'up' || local.age_s === null || local.age_s > devices.sample_s * 3
            || (local.session_memory.age_s ?? 0) > 30 };
}
export function resource_readout(rows: readonly OfficeResource[], expected = 1, stale = false): string {
    const cpu = rows.length && rows.every(row => row.cpu_pct !== null) ? `${rows.reduce((sum, row) => sum + row.cpu_pct!, 0).toFixed(0)}%` : '—';
    const bytes = rows.reduce((sum, row) => sum + row.rss, 0);
    const ram = !rows.length ? '—' : bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(1)} GiB` : `${Math.round(bytes / 1024 ** 2)} MiB`;
    const coverage = rows.length < expected ? ` · ${rows.length}/${expected} sampled` : '';
    return `CPU ${cpu} · RAM ${ram}${coverage}${stale ? ' · stale' : ''}`;
}
