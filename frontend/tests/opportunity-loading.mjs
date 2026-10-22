import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import vm from 'node:vm';
import ts from 'typescript';

// Exercise the real hook with a deterministic hook/timer runtime, without a server.
const slots = [];
let cursor = 0;
let timers = new Map();
let timerId = 0;
const calls = [];
const react = {
  useState(initial) {
    const i = cursor++;
    slots[i] ??= { value: initial };
    return [slots[i].value, value => { slots[i].value = value; }];
  },
  useRef(initial) {
    const i = cursor++;
    return slots[i] ??= { current: initial };
  },
  useEffect(fn, deps) {
    const i = cursor++;
    if (!slots[i] || deps.some((d, n) => !Object.is(d, slots[i].deps[n]))) {
      slots[i]?.cleanup?.();
      slots[i] = { deps, cleanup: fn() };
    }
  },
};
const require = createRequire(import.meta.url);
function load(relative, overrides = {}) {
  const source = readFileSync(new URL(relative, import.meta.url), 'utf8');
  const output = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText;
  const exports = {};
  vm.runInNewContext(output, {
    exports, require: name => overrides[name] ?? require(name), URLSearchParams,
    AbortController, console,
    setTimeout: fn => { timers.set(++timerId, fn); return timerId; },
    clearTimeout: id => timers.delete(id),
  });
  return exports;
}
const { filterParams } = load('../src/lib/api.ts');
const { emptyFilters } = load('../src/lib/types.ts');
const { useOpportunities } = load('../src/hooks/useApi.ts', {
  react,
  '@/lib/api': { filterParams, api: {
    opportunities(filters, signal) {
      return new Promise(resolve => calls.push({ filters, signal, resolve }));
    },
  } },
});
function render(filters = { ...emptyFilters }, refresh = 0) {
  cursor = 0;
  return useOpportunities(filters, refresh);
}
function tick() {
  const pending = [...timers.values()];
  timers.clear();
  pending.forEach(fn => fn());
}
async function settle() { for (let i = 0; i < 5; i++) await Promise.resolve(); }

render(); tick();
for (let i = 0; i < 20; i++) { render(); tick(); }
assert.equal(calls.length, 1, 'Unchanged filters and progress renders must not refetch');
const first = { items: [], total: 10 };
calls[0].resolve(first); await settle();
assert.equal(render().loading, false);
assert.equal(render().data, first);
tick(); assert.equal(calls.length, 1, 'Response state updates must not cause a request loop');

const page2 = { ...emptyFilters, page: 2 };
render(page2); tick();
assert.equal(calls.length, 2);
assert.equal(calls[0].signal.aborted, true);
const search = { ...page2, search: 'health' };
render(search); tick();
assert.equal(calls[1].signal.aborted, true);
calls[1].resolve({ items: [], total: 999 }); await settle();
assert.equal(render(search).data, first, 'An obsolete response must not replace the current data');
assert.equal(render(search).loading, true);
calls[2].resolve({ items: [], total: 3 }); await settle();
assert.equal(render(search).data.total, 3);
assert.equal(render(search).loading, false);
render(search, 1); tick();
assert.equal(calls.length, 4, 'Explicit refresh must still reload');
slots.forEach(slot => slot?.cleanup?.());
assert.equal(calls[3].signal.aborted, true, 'Unmount must cancel the request');
console.log('PASS: stable renders, response updates, pagination, search, stale responses, refresh, cancellation');
