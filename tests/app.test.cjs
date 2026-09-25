const test = require('node:test');
const assert = require('node:assert/strict');
const {harness, state} = require('./app_harness.cjs');
const post = text => ({id: 1, text, date: '2025-01-01', media_kind: 'text'});
const list = (revision, text, channel = 'main') => ({posts: [post(text)], total: 1, active_channel: channel, library_revision: revision});

test('TS-017: rendered downloads retain main/child library and one download query', () => {
  for (const channel of ['main', 'a'.repeat(32)]) {
    const h = harness(); h.app.setState(state('1', channel));
    h.app.renderPostDetail({id: 1, text: 'fixture', media_kind: 'file', media_url: '/api/media/1', media_name: 'fixture.bin', revisions: []});
    const link = h.get('post-detail-body').querySelectorAll('a').find(n => n.download === 'fixture.bin');
    const url = new URL(link.href, 'http://127.0.0.1');
    assert.equal(url.searchParams.get('library'), channel);
    assert.equal(url.searchParams.get('download'), '1');
  }
});

test('TS-023: two stale forms patch only changed fields; reverted/no-op/blank hash', async () => {
  const shared = state();
  const responder = (url, init) => {
    if (url === '/api/settings') { Object.assign(shared.settings, JSON.parse(init.body)); return {settings: shared.settings}; }
    return url === '/api/state' ? shared : list(shared.library.revision, 'current');
  };
  const a = harness(responder), b = harness(responder);
  for (const h of [a, b]) { h.app.setState(structuredClone(shared)); h.app.fillSettings(); }
  a.get('download-media').checked = false;
  await a.app.saveSettings({preventDefault() {}});
  b.get('archive-before-sync').checked = false;
  await b.app.saveSettings({preventDefault() {}});
  assert.equal(shared.settings.download_media, false); assert.equal(shared.settings.archive_before_sync, false);
  assert.deepEqual(JSON.parse(b.requests.find(r => r.url === '/api/settings').init.body), {archive_before_sync: false});
  const before = b.requests.length;
  b.get('capture-context').checked = false; b.get('capture-context').checked = true;
  await b.app.saveSettings({preventDefault() {}});
  assert.equal(b.requests.length, before);
});

test('TS-023: validation failure keeps edits pending and credential patch omits untouched fields', async () => {
  let fail = true;
  const h = harness((url, init) => url === '/api/settings' ? fail ? {ok:false, error:'invalid'} : {settings: state().settings} : url === '/api/state' ? state() : list('epoch:1','row'));
  h.app.setState(state()); h.app.fillSettings(); h.get('api-id').value = '2'; h.get('api-hash').value = 'b'.repeat(32);
  await h.app.saveSettings({preventDefault() {}});
  assert.equal(h.get('api-hash').value, 'b'.repeat(32));
  fail = false; await h.app.saveSettings({preventDefault() {}});
  const writes = h.requests.filter(r => r.url === '/api/settings');
  assert.equal(writes.length, 2);
  assert.deepEqual(JSON.parse(writes[1].init.body), {api_id: 2, api_hash: 'b'.repeat(32)});
});

test('TS-025/026: equal job counters, failed refresh retry and Watch media completion', async () => {
  let current = state(), fail = false, requests = 0;
  const h = harness(url => {
    if (url === '/api/state') return structuredClone(current);
    requests++; if (fail) { fail = false; throw new Error('temporary posts failure'); }
    return list(current.library.revision, current.library.revision);
  });
  await h.app.loadState(); assert.equal(requests, 1);
  current.library.revision = 'epoch:2'; fail = true;
  await h.app.loadState(); assert.equal(h.app.signature(), JSON.stringify(['main','epoch:1']));
  await h.app.loadState(); assert.equal(requests, 3); assert.equal(h.app.signature(), JSON.stringify(['main','epoch:2']));
  await h.app.loadState(); assert.equal(requests, 3);
  current.job.running = true; current.library.revision = 'epoch:3';
  await h.app.loadState(); assert.equal(requests, 4);
  current.library.revision = 'epoch:4'; current.job.media_downloaded = 1;
  await h.app.loadState(); assert.equal(requests, 5);
  await h.app.loadState(); assert.equal(requests, 5);
});

test('TS-026: superseded, aborted and wrong-library responses cannot acknowledge', async () => {
  const waiting = [];
  const h = harness(() => new Promise(resolve => waiting.push(resolve)));
  h.app.setState(state());
  const older = h.app.loadPosts();
  h.app.filters.q = 'new filter'; const newer = h.app.loadPosts();
  waiting[1](list('epoch:2','new')); await newer;
  waiting[0](list('epoch:1','old')); await older;
  assert.equal(h.app.signature(), JSON.stringify(['main','epoch:2']));
  const wrong = h.app.loadPosts(); waiting[2](list('epoch:99','wrong','other')); await wrong;
  assert.equal(h.app.signature(), JSON.stringify(['main','epoch:2']));
  const previous = h.app.loadPosts(); h.app.setState(state('other:1','other')); h.app.resetChannelView();
  waiting[3](list('epoch:100','old library')); await previous;
  assert.equal(h.app.signature(), '');
});

test('TS-022: duplicate Sync only offers an explicit Open request and never retries Sync', async () => {
  let current = state();
  const h = harness((url, init) => {
    if (url === '/api/jobs') return {capture_started:false, existing_archive_ids:['existing']};
    if (url === '/api/channels/select') { current = state('other:1','existing'); return {active_channel:'existing'}; }
    return url === '/api/state' ? current : list(current.library.revision,'row',current.active_channel);
  });
  h.app.setState(current);
  assert.equal(await h.app.startJob('sync'), false);
  assert.equal(h.app.getState().active_channel, 'main');
  assert.equal(h.requests.filter(r => r.url === '/api/channels/select').length, 0);
  await h.get('existing-archives').querySelector('button').events.click();
  assert.equal(h.app.getState().active_channel, 'existing');
  assert.equal(h.requests.filter(r => r.url === '/api/jobs').length, 1);
});
