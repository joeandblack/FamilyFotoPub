// The library destination is chosen once, in Settings. A first import only checks that it is set,
// the same way the first upload does, and never asks for or saves a destination itself.
export function setupImportStorage(container, api) {
  async function ensureDestination() {
    const settings = await api('/api/storage/settings');
    if (!settings.destination) throw Error('Choose the library destination in Settings before the first import.');
  }
  return {setStorage() {}, ensureDestination};
}
