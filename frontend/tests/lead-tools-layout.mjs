import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import vm from 'node:vm';
import ts from 'typescript';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

const require = createRequire(import.meta.url);
const source = readFileSync(new URL('../src/components/LeadTools.tsx', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, { compilerOptions: {
  target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
} }).outputText;
const events = [];
function load(active = null) {
  let slot = 0;
  const exports = {};
  vm.runInNewContext(compiled, {
    exports,
    window: { dispatchEvent: event => events.push(event) },
    CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options.detail; } },
    require(name) {
      if (name === 'react') return {
        useState: initial => [slot++ === 0 ? active : initial, () => {}],
        useRef: () => ({ current: null }), useEffect() {},
      };
      if (name.endsWith('.css')) return {};
      if (name === './DashboardLeads') return {
        DashboardLeads: () => createElement('div', { 'data-store': 'saved-leads' }),
      };
      if (name === './DirectoryPanel') return {
        DirectoryPanel: props => createElement('div', { 'data-tool': 'directories', 'data-open': props.initiallyOpen }),
      };
      if (name === './LeadIntake') return {
        LeadFollowUps: () => createElement('div', { 'data-tool': 'follow-ups' }),
        ManualLeadForm: props => createElement('div', { 'data-tool': 'intake', 'data-open': props.initiallyOpen, 'data-read-only': props.readOnly }),
      };
      return require(name);
    },
  });
  return exports;
}
function render(active, readOnly = false) {
  return renderToStaticMarkup(load(active).LeadTools({ isAdmin: true, name: 'Reviewer', readOnly }));
}
const initial = render(null);
assert.match(initial, /aria-label="Lead tools"/);
assert.doesNotMatch(initial.match(/<dialog\b[^>]*>/)[0], /\sopen(?:[=>\s])/,
  'Tools must not open over the opportunity dashboard on arrival');
assert.match(initial, /hidden=""[\s\S]*?data-store="saved-leads"/,
  'The saved-lead store must stay mounted without taking up dashboard space');
assert.doesNotMatch(initial, /data-tool=/, 'Intake, directories and follow-ups must be absent until requested');
assert.match(render('my-leads'), /data-tool="follow-ups"/);
assert.doesNotMatch(render('my-leads'), /data-tool="(?:directories|intake)"/);
assert.match(render('directories'), /data-tool="directories" data-open="true"/);
assert.doesNotMatch(render('directories'), /data-tool="(?:follow-ups|intake)"/);
assert.match(render('add-lead', true), /data-tool="intake" data-open="true" data-read-only="true"/);
const { AddLeadButton } = load();
const button = AddLeadButton({});
button.props.onClick();
assert.equal(events[0].type, 'open-lead-tools');
assert.equal(events[0].detail, 'add-lead');
assert.equal(AddLeadButton({ readOnly: true }).props.disabled, true);
console.log('Lead tools layout: closed by default, on-demand panels, saved-lead store and add-lead action passed');
