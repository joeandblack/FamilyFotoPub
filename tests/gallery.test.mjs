import test from 'node:test';
import assert from 'node:assert/strict';
import { updateGallery } from '../web/gallery.mjs';

function fixture() {
  const grid = {
    children: [],
    addEventListener() {},
    get firstElementChild() { return this.children[0] || null; },
    replaceChildren() { this.children = []; },
    insertBefore(node, cursor) {
      node.remove();
      const index = cursor ? this.children.indexOf(cursor) : this.children.length;
      this.children.splice(index, 0, node);
      node.parent = this;
    },
    ownerDocument: {
      body: {append() {}},
      addEventListener() {},
      defaultView: {addEventListener() {}},
      createElement() {
        return { set innerHTML(html) {
          this.content = { firstElementChild: {
            html, parent: null,
            get nextElementSibling() {
              return this.parent.children[this.parent.children.indexOf(this) + 1] || null;
            },
            remove() {
              if (this.parent) {
                this.parent.children.splice(this.parent.children.indexOf(this), 1);
                this.parent = null;
              }
            },
            replaceWith(node) {
              this.parent.insertBefore(node, this);
              this.remove();
            }
          } };
        } };
      }
    }
  };
  return grid;
}

test('polling preserves unchanged image nodes and updates only changed tiles', () => {
  const grid = fixture();
  const tiles = [{ id: '1', html: 'photo' }, { id: '2', html: 'video' }];
  updateGallery(grid, tiles);
  const [photo, video] = grid.children;
  updateGallery(grid, tiles);
  assert.equal(grid.children[0], photo);
  assert.equal(grid.children[1], video);
  updateGallery(grid, [tiles[0], { id: '2', html: 'video with poster' }]);
  assert.equal(grid.children[0], photo);
  assert.notEqual(grid.children[1], video);
  updateGallery(grid, [{ id: '2', html: 'video with poster' }, tiles[0]]);
  assert.equal(grid.children[1], photo);
  updateGallery(grid, [tiles[0]]);
  assert.deepEqual(grid.children, [photo]);
});
