// Types for evals-export-source.mjs, so the test that imports it is checked like any other.
export const REPO: URL;
export const EXPORT_PAGE: URL;
export const SOURCE_META: string;
export const FIGURES: string[];
export const THEMES: string[];
export const FIGURE_BLOCK: Record<string, string>;
export const FIGURES_MANIFEST: URL;
export function figures_for(results: unknown): string[];
export function figure_files(figures?: readonly string[]): string[];
export function sha256_file(url: URL): string;
export function evals_source_files(): string[];
export function evals_source_hash(): string;
