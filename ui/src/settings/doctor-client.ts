/**
 * The page's side of `GET /doctor`: what this machine has, section by section, as `tldrack doctor`
 * prints it. The bridge runs the checks; the page draws them and offers the one fix it can press.
 *
 * AN ACTION IS A CLOSED SET. A check may carry `action`, and the page turns a kind it knows into a
 * button. A kind it does not know is read as no action, so a newer bridge's fix shows as its `fix`
 * text and never as a button that posts somewhere this page did not choose.
 *
 *   install-hooks  POST /profiles/install-hooks {"profile": <id>}: the same merge as
 *                  `tldrack profiles --install-hooks` (`bridge/tldrack/main.py` owns the reply).
 */
import { z } from 'zod';

import { bridge_read, bridge_write, type BridgeResult } from './bridge-call.ts';

export const DOCTOR_LEVELS = ['ok', 'warn', 'fail', 'off'] as const;
export type DoctorLevel = (typeof DOCTOR_LEVELS)[number];

/** The mark a level is drawn with, and the word a screen reader says for it. */
export const LEVEL_MARKS: Readonly<Record<DoctorLevel, string>> = { ok: '✓', warn: '!', fail: '✗', off: '·' };
export const LEVEL_WORDS: Readonly<Record<DoctorLevel, string>> = { ok: 'ok', warn: 'warning', fail: 'failed', off: 'off' };

export type DoctorAction = { kind: 'install-hooks'; profile: string };

const install_hooks_action = z.object({ kind: z.literal('install-hooks'), profile: z.string().min(1).max(120) });

/** Whatever a check carried as `action` → an action this page can press, or null. */
export function read_action(raw: unknown): DoctorAction | null {
    const parsed = install_hooks_action.safeParse(raw);
    return parsed.success ? { kind: parsed.data.kind, profile: parsed.data.profile } : null;
}

// `action` is read on the whole check, not as a field transform: a check that leaves the key out
// is as valid as one that sends null, and a transform on the field alone would make the key required.
const check_schema = z
    .object({
        name: z.string().max(200),
        level: z.enum(DOCTOR_LEVELS),
        detail: z.string().max(2000).default(''),
        fix: z.string().max(2000).default(''),
        action: z.unknown().optional(),
    })
    .transform((check) => ({ ...check, action: read_action(check.action) }));
export type DoctorCheck = z.infer<typeof check_schema>;

const section_schema = z.object({ title: z.string().max(200), checks: z.array(check_schema).max(200) });
export type DoctorSection = z.infer<typeof section_schema>;

const doctor_schema = z.object({
    sections: z.array(section_schema).max(100),
    exit: z.number().int(),
    verdict: z.string().max(600),
});
export type DoctorReport = z.infer<typeof doctor_schema>;

/** A `/doctor` body → the report, or null when it is not one. */
export function parse_doctor(body: unknown): DoctorReport | null {
    const parsed = doctor_schema.safeParse(body);
    return parsed.success ? parsed.data : null;
}

/** How many checks stand at each level, for the line beside the verdict. */
export function doctor_counts(report: DoctorReport): Record<DoctorLevel, number> {
    const counts: Record<DoctorLevel, number> = { ok: 0, warn: 0, fail: 0, off: 0 };
    for (const section of report.sections) for (const check of section.checks) counts[check.level] += 1;
    return counts;
}

/** The verdict's level: the report's own exit code decides, then any warning. */
export function verdict_level(report: DoctorReport): DoctorLevel {
    if (report.exit !== 0) return 'fail';
    return doctor_counts(report).warn > 0 ? 'warn' : 'ok';
}

/** The fix to print under a check: nothing for one that is ok. */
export function fix_of(check: DoctorCheck): string {
    return check.level === 'ok' ? '' : check.fix;
}

/** A stable key for one check's button and its message: a profile id is unique among actions. */
export function action_key(action: DoctorAction): string {
    return `${action.kind}:${action.profile}`;
}

/** The checks shell out for a tool's version, so this read waits longer than a plain one. */
const DOCTOR_TIMEOUT_MS = 20_000;

export function fetch_doctor(): Promise<BridgeResult<DoctorReport>> {
    return bridge_read('/doctor', doctor_schema, 'a doctor report', DOCTOR_TIMEOUT_MS);
}

const install_hooks_reply = z.object({
    ok: z.literal(true),
    changed: z.boolean(),
    message: z.string().max(600),
    backup: z.string().max(4096).nullish(),
    notes: z.array(z.string().max(600)).max(50).default([]),
});
export type InstallHooksReply = z.infer<typeof install_hooks_reply>;

/** A 200 body of `POST /profiles/install-hooks` → the reply, or null. */
export function parse_install_hooks(body: unknown): InstallHooksReply | null {
    const parsed = install_hooks_reply.safeParse(body);
    return parsed.success ? parsed.data : null;
}

export function install_hooks(profile: string): Promise<BridgeResult<InstallHooksReply>> {
    return bridge_write('/profiles/install-hooks', { profile }, install_hooks_reply, 'a hook install reply');
}
