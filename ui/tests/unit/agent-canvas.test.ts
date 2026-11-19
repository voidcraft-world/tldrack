import test from 'node:test';
import assert from 'node:assert/strict';
import {
    CANVAS_MESSAGE_TYPE,
    CANVAS_MIN_COLUMN_PX,
    CANVAS_MIN_UNDER_PX,
    CANVAS_UNDER_RATIO,
    DEFAULT_CANVAS_LAYOUT,
    RAIL_KEEP_PX,
    canvas_layout_label,
    canvas_page_url,
    clamp_canvas_column_px,
    clamp_canvas_under_px,
    next_canvas_layout,
    parse_canvas_hello,
    parse_canvas_layout,
    parse_canvas_sizes,
} from '../../src/agent-rack/agent-canvas.ts';
import { TERM_MIN_PX } from '../../src/agent-rack/rack-window-geometry.ts';

test('layout: a stored value round-trips, anything else is the default', () => {
    assert.equal(parse_canvas_layout('column'), 'column');
    assert.equal(parse_canvas_layout('off'), 'off');
    assert.equal(parse_canvas_layout(null), DEFAULT_CANVAS_LAYOUT);
    assert.equal(parse_canvas_layout('sideways'), DEFAULT_CANVAS_LAYOUT);
    assert.equal(DEFAULT_CANVAS_LAYOUT, 'under');
});

test('layout: the chip cycles through every placement and back', () => {
    assert.equal(next_canvas_layout('under'), 'column');
    assert.equal(next_canvas_layout('column'), 'off');
    assert.equal(next_canvas_layout('off'), 'under');
    assert.equal(canvas_layout_label('column'), 'COLUMN');
});

test('page url: only for a valid focused session, encoded, origin trailing slash tolerated', () => {
    assert.equal(
        canvas_page_url('http://127.0.0.1:7682/', 'wt-fix_nav'),
        'http://127.0.0.1:7682/plugin/agent-canvas/page?session=wt-fix_nav',
    );
    assert.equal(canvas_page_url('http://127.0.0.1:7682', null), null);
    assert.equal(canvas_page_url('http://127.0.0.1:7682', '../x'), null);
    assert.equal(canvas_page_url('http://127.0.0.1:7682', 'a b'), null);
});

test('sizes: corrupt or missing storage reads as no opinion', () => {
    assert.deepEqual(parse_canvas_sizes(null), { under_px: null, column_px: null });
    assert.deepEqual(parse_canvas_sizes('{not json'), { under_px: null, column_px: null });
    assert.deepEqual(parse_canvas_sizes('{"under_px": 300, "column_px": "wide"}'), { under_px: 300, column_px: null });
    assert.deepEqual(parse_canvas_sizes('{"under_px": -5, "column_px": 400}'), { under_px: null, column_px: 400 });
});

test('under: no opinion takes the ratio; a drag is clamped between both floors', () => {
    assert.equal(clamp_canvas_under_px(Number.NaN, 1000), 1000 * CANVAS_UNDER_RATIO);
    assert.equal(clamp_canvas_under_px(10, 1000), CANVAS_MIN_UNDER_PX);
    assert.equal(clamp_canvas_under_px(5000, 1000), 1000 - RAIL_KEEP_PX);
});

test('under: a column too short for both floors keeps the list its floor', () => {
    assert.equal(clamp_canvas_under_px(300, RAIL_KEEP_PX + 50), 50);
    assert.equal(clamp_canvas_under_px(300, 100), 0);
});

test('column: the terminal always keeps its floor', () => {
    const rack = 400;
    const width = 1800;
    assert.equal(clamp_canvas_column_px(5000, width, rack, TERM_MIN_PX), width - rack - TERM_MIN_PX);
    assert.equal(clamp_canvas_column_px(10, width, rack, TERM_MIN_PX), CANVAS_MIN_COLUMN_PX);
    assert.equal(clamp_canvas_column_px(Number.NaN, width, rack, TERM_MIN_PX), width * 0.3);
    // A window too narrow for a column still yields a legal (minimum) size.
    assert.equal(clamp_canvas_column_px(600, 900, rack, TERM_MIN_PX), CANVAS_MIN_COLUMN_PX);
});

test('hello: only the canvas message shape is accepted, questions are bounded', () => {
    assert.equal(parse_canvas_hello(null), null);
    assert.equal(parse_canvas_hello({ type: 'other', session: 'a' }), null);
    assert.equal(parse_canvas_hello({ type: CANVAS_MESSAGE_TYPE }), null);
    // Legacy (step 6.8): a canvas page one release behind still says hello under its old name.
    assert.equal(parse_canvas_hello({ type: 'voidcraft-agent-canvas', session: 'a' })?.session, 'a');
    assert.equal(parse_canvas_hello({ type: 'other-agent-canvas', session: 'a' }), null);
    assert.deepEqual(parse_canvas_hello({ type: CANVAS_MESSAGE_TYPE, session: 'a', has_canvas: true, question: '' }), {
        session: 'a',
        has_canvas: true,
        has_content: true,
        question: null,
    });
    const long = parse_canvas_hello({ type: CANVAS_MESSAGE_TYPE, session: 'a', question: 'x'.repeat(500) });
    assert.equal(long?.question?.length, 120);
    assert.equal(parse_canvas_hello({ type: CANVAS_MESSAGE_TYPE, session: 'a', has_content: false })?.has_content, false);
    assert.equal(parse_canvas_hello({ type: CANVAS_MESSAGE_TYPE, session: 'a', has_content: 'false' })?.has_content, true);
});
