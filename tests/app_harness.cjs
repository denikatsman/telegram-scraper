const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

class Node {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {}; this.attributes = {};
    this.value = ''; this.checked = false; this.hidden = false; this.textContent = ''; this.events = {};
    this.style = {}; this.classList = { toggle() {}, add() {}, remove() {}, contains() { return false; } };
  }
  get value() { return this._value; }
  set value(value) { this._value = String(value); }
  get childNodes() { return this.children; }
  append(...nodes) { this.children.push(...nodes); }
  prepend(...nodes) { this.children.unshift(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute(key, value) { this.attributes[key] = value; }
  getAttribute(key) { return this.attributes[key]; }
  removeAttribute(key) { delete this.attributes[key]; }
  addEventListener(name, callback) { this.events[name] = callback; }
  querySelectorAll(selector) { return this.children.flatMap(n => n instanceof Node ? [...(n.tagName.toLowerCase() === selector ? [n] : []), ...n.querySelectorAll(selector)] : []); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  contains() { return false; }
  focus() {} scrollIntoView() {} close() { this.open = false; } showModal() { this.open = true; }
}

function state(revision = 'epoch:1', channel = 'main') {
  return { active_channel: channel, csrf_token: 'fixture', channels: [{id: channel, name: 'Fixture'}],
    settings: { api_id: 1, api_hash_set: true, channel: '@fixture', download_media: true, archive_before_sync: true, capture_context: true },
    library: { revision, total: 1, last_date: '2025-01-01', channel_title: 'Fixture' },
    job: { running: false, status: 'idle', added: 0, updated: 1 }, connection: { authorized: true }, health: {runs: []} };
}

function harness(responder = () => ({})) {
  const nodes = new Map();
  const get = id => { if (!nodes.has(id)) nodes.set(id, new Node()); return nodes.get(id); };
  const document = { getElementById: get, createElement: tag => new Node(tag), createElementNS: (_, tag) => new Node(tag),
    createTextNode: text => Object.assign(new Node('#text'), {textContent: text}),
    querySelectorAll: () => [], querySelector: () => null, addEventListener() {}, documentElement: new Node(), body: new Node(), activeElement: null };
  const requests = [];
  const context = vm.createContext({ document, window: {location: {origin: 'http://127.0.0.1', protocol: 'http:'}, addEventListener() {}},
    URL, URLSearchParams, AbortController, Intl, setTimeout: () => 1, clearTimeout() {}, requestAnimationFrame: f => f(),
    fetch: async (url, init) => { requests.push({url, init}); const value = await responder(url, init); return {ok: value?.ok !== false, json: async () => value}; } });
  const filename = path.join(__dirname, '../telegram_scraper/static/app.js');
  let source = fs.readFileSync(filename, 'utf8');
  source = source.replace('  loadState();\n})();', `  globalThis.testApp = { fillSettings, saveSettings, loadState, loadPosts, renderPostDetail, startJob, offerExistingArchives, resetChannelView,
    setState(value) { state = value; }, getState() { return state; }, filters,
    signature() { return lastLibrarySignature; }, loaded() { return postsLoaded; } };\n})();`);
  vm.runInContext(source, context, {filename});
  return { app: context.testApp, get, requests, nodes };
}

module.exports = { harness, state, Node };
