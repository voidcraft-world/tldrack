import { onBeforeUnmount, onMounted, ref, type Ref } from 'vue';

/**
 * An element's width in CSS pixels, kept current. The charts are drawn at the size they are
 * shown, never scaled by a `viewBox`: scaling a chart down scales its text under the page's
 * type floors (`evals.css` owns the floors).
 */
export function use_width(element: Ref<HTMLElement | null>, fallback: number): Ref<number> {
    const width = ref(fallback);
    let observer: ResizeObserver | null = null;
    const measure = (): void => {
        const measured = element.value?.clientWidth ?? 0;
        if (measured > 0) width.value = measured;
    };
    onMounted(() => {
        measure();
        if (element.value && typeof ResizeObserver !== 'undefined') {
            observer = new ResizeObserver(measure);
            observer.observe(element.value);
        }
    });
    onBeforeUnmount(() => observer?.disconnect());
    return width;
}
