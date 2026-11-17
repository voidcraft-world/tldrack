/** Supersample the compact office, including glyph interiors that MSAA cannot smooth.
 * Keep a larger pop-out within eight million pixels and the GPU's buffer/viewport limits.
 */
export function office_pixel_ratio(width: number, height: number, device_ratio: number, max_dimension: number): number {
    const w = Math.max(1, width), h = Math.max(1, height);
    const desired = Math.min(4, Math.max(2, device_ratio * 1.5));
    return Math.min(desired, Math.sqrt(8_000_000 / (w * h)), max_dimension / Math.max(w, h));
}
