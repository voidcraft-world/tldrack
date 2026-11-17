import { z } from 'zod';

const path_schema = z.string().min(1).max(4096).refine((path) => !/[\x00-\x1f\x7f]/.test(path));
export const launch_directory_schema = z.object({
    label: z.string().trim().min(1).max(40),
    path: path_schema,
});
export const launch_directories_schema = z.object({
    directories: z.array(launch_directory_schema).min(1).max(16),
    default_path: path_schema,
    bridge_path: path_schema,
}).refine((state) => state.directories.some((entry) => entry.path === state.default_path));

export type LaunchDirectory = z.infer<typeof launch_directory_schema>;
export type LaunchDirectories = z.infer<typeof launch_directories_schema>;
