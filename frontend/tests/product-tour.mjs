// The guided tour drives the real dashboard, so it must never be able to
// create a Wrike task or save anything. Run: node tests/product-tour.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';

const read = name => readFileSync(new URL(`../src/components/${name}`, import.meta.url), 'utf8');
const engine = read('ProductTour.tsx');
const script = read('tourSteps.ts');

for (const [name, source] of [['ProductTour.tsx', engine], ['tourSteps.ts', script]]) {
  assert.ok(!/createWrikeTask|\/tasks|fetch\(|api\./.test(source), `${name} must make no API calls`);
  const out = ts.transpileModule(source, { compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 }, reportDiagnostics: true });
  assert.equal((out.diagnostics || []).length, 0, `${name} compiles`);
}

// Split the script into its steps (each starts with `id: "..."`).
const steps = script.split(/\n\s*\{\n\s*id: "/).slice(1).map(s => ({ id: s.slice(0, s.indexOf('"')), text: s }));
assert.ok(steps.length >= 15, 'the tour has its steps');
const byId = Object.fromEntries(steps.map(s => [s.id, s.text]));

// The step on "Yes, create task" only points at it: no click, no action.
assert.ok(byId['wrike-yes'], 'the push step exists');
assert.ok(!/\bclick:|\baction:/.test(byId['wrike-yes']), 'the tour never clicks Yes, create task');

// Nothing in the script clicks the create button, Save lead or Mark reviewed.
for (const s of steps) {
  const clicks = (s.text.match(/click:[^\n]*/g) || []).join(' ');
  assert.ok(!/ud-primary-button|Yes, create|dl-save-button|Mark reviewed/i.test(clicks), `step ${s.id} clicks something that writes`);
}

// Opening a lead must not record a "viewed" event: the click is replaced.
assert.match(byId['open-lead'], /action: \(\) => \{\}/);

// The assignee steps explain who can be assigned and that it is optional.
assert.match(script, /Only people registered on this platform are listed/);
assert.match(script, /Assigning is optional/);

// While the tour runs the visitor's own clicks are blocked by a shield.
assert.match(engine, /className="gt-shield"/);
console.log('product-tour: ok');
