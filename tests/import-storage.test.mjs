import test from 'node:test';
import assert from 'node:assert/strict';
import {setupImportStorage} from '../web/import-storage.mjs';

function fixture(settings) {
  const calls = [];
  const api = async (url, options) => {
    calls.push({url, options});
    return {...settings};
  };
  return {storage: setupImportStorage({append() {throw Error('The import form no longer asks for a destination');}}, api), calls};
}

test('an import starts when the library destination is already set', async () => {
  const f = fixture({destination: 'E:/Library', revision: 'one'});
  await f.storage.ensureDestination('F:/Photos');
  assert.deepEqual(f.calls, [{url: '/api/storage/settings', options: undefined}]);
});

for (const source of ['', 'F:/Photos']) {
  test(`${source ? 'a server import' : 'an import without a source'} is refused, pointing to Settings, when no destination is set`, async () => {
    const f = fixture({destination: '', revision: 'one'});
    await assert.rejects(f.storage.ensureDestination(source), /Choose the library destination in Settings before the first import/);
    assert.equal(f.calls.length, 1, 'only reads the settings; it never saves a destination');
    assert.equal(f.calls[0].options, undefined);
  });
}

test('the import form keeps the same storage interface', () => {
  const {storage} = fixture({destination: '', revision: 'one'});
  assert.equal(typeof storage.setStorage, 'function');
  storage.setStorage({destination: ''});
});
