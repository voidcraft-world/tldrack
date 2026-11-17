import { test } from 'node:test';
import assert from 'node:assert/strict';
import { activity_board_layout, type BoardRect } from '../../src/agent-rack/activity-map-layout.ts';
function overlaps(a: BoardRect, b: BoardRect): boolean {
    return a.x < b.x + b.width && a.x + a.width > b.x && a.y < b.y + b.height && a.y + a.height > b.y;
}
test('the measured viewport is filled without overlapping tiles at different pane shapes', () => {
    for (const [width, height] of [[832, 510], [700, 240], [500, 700], [384, 440], [250, 220], [1200, 300]]) {
        const board = activity_board_layout({ width, height });
        assert.equal(board.width, width);
        assert.equal(board.height, height);
        assert.equal(new Set(board.tiles.map(tile => tile.id)).size, 10);
        const rects = [...board.tiles, board.core];
        for (const rect of rects) {
            assert.ok(rect.width > 0 && rect.height > 0);
            assert.ok(rect.x >= board.margin - 0.01 && rect.y >= board.header - 0.01);
            assert.ok(rect.x + rect.width <= width - board.margin + 0.01);
            assert.ok(rect.y + rect.height <= height - board.footer + 0.01);
            for (const other of rects) if (rect !== other) assert.equal(overlaps(rect, other), false);
        }
        assert.ok(Math.abs(Math.max(...board.tiles.map(tile => tile.y + tile.height)) - (height - board.footer)) < 0.01);
    }
});
test('height and width changes redistribute the drawing instead of adding letterboxing', () => {
    const small = activity_board_layout({ width: 832, height: 300 });
    const tall = activity_board_layout({ width: 832, height: 510 });
    assert.ok(tall.tiles[0].height > small.tiles[0].height);
    assert.equal(small.mode, 'circuit');
    assert.equal(activity_board_layout({ width: 400, height: 600 }).mode, 'grid');
    assert.equal(activity_board_layout({ width: NaN, height: Infinity }).width, 720);
});
