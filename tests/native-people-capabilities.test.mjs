import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';

const source = await readFile(new URL('../web/native-people-catalog.mjs', import.meta.url), 'utf8');
for (const calibrated of [false, true]) {
  test(`native capability guard, calibrated=${calibrated}`, async () => {
    const labels = ['Auto-organize faces', 'Detect again', 'Move to person', 'Start / Resume'];
    const buttons = labels.map(textContent => ({textContent, disabled: false}));
    let changed;
    globalThis.fetch = async () => ({ok: true, json: async () => ({automatic_matching: calibrated})});
    globalThis.document = {body: {}, querySelectorAll: () => buttons};
    globalThis.MutationObserver = class {
      constructor(callback) { changed = callback; }
      observe() {}
    };
    try {
      await import(`data:text/javascript,${encodeURIComponent(source)}#${calibrated}`);
      assert.equal(buttons[0].disabled, !calibrated);
      assert.equal(buttons[1].disabled, !calibrated);
      assert.equal(buttons[2].disabled, false);
      assert.equal(buttons[3].disabled, false);
      buttons[0].disabled = false;
      changed();
      assert.equal(buttons[0].disabled, !calibrated);
      if (!calibrated) assert.match(buttons[0].title, /calibration/);
    } finally {
      delete globalThis.document;
      delete globalThis.MutationObserver;
      delete globalThis.fetch;
    }
  });
}
