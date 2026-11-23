// The product tour must never be able to create a Wrike task: it is a demo.
// Run: node tests/product-tour.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';

const source = readFileSync(new URL('../src/components/ProductTour.tsx', import.meta.url), 'utf8');

// No call to the create endpoint, and no POST of any kind.
assert.ok(!/createWrikeTask/.test(source), 'tour must not call api.createWrikeTask');
assert.ok(!/\/tasks/.test(source), 'tour must not reference the Wrike tasks endpoint');
assert.ok(!/method:\s*["']POST/i.test(source), 'tour must not send POST requests');

// The practice "Yes, create task" button is always disabled.
const yes = source.match(/<button[^>]*>Yes, create task<\/button>/);
assert.ok(yes, 'practice confirm button exists');
assert.match(yes[0], /\sdisabled[\s>]/, 'practice confirm button is disabled');

// The only Wrike call is the read-only member list.
const wrikeCalls = [...source.matchAll(/api\.(wrike\w*)\(/g)].map(m => m[1]);
assert.deepEqual([...new Set(wrikeCalls)], ['wrikeAssignees']);

// The assignee step explains who can be assigned.
assert.match(source, /Only people registered on this platform appear/);
assert.match(source, /Add to folder without assignee/);

// It compiles as TSX.
const out = ts.transpileModule(source, { compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 }, reportDiagnostics: true });
assert.equal((out.diagnostics || []).length, 0);
console.log('product-tour: ok');
