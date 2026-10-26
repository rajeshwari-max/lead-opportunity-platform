import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import vm from 'node:vm';
import ts from 'typescript';
import { renderToStaticMarkup } from 'react-dom/server';

// Render the real workspace in its loaded state without a browser or server.
const require = createRequire(import.meta.url);
const source = readFileSync(new URL('../src/components/PersonalWorkspace.tsx', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
}).outputText;

function stageChoices(journeys) {
  let slot = 0;
  const exports = {};
  vm.runInNewContext(compiled, {
    exports, URLSearchParams, location: { search: '' },
    require(name) {
      if (name === 'react') return {
        useEffect() {},
        useState(initial) {
          const index = slot++;
          return [index === 0 ? { unlocked: true, owner: 'reviewer@example.org' }
            : index === 2 ? journeys : initial, () => {}];
        },
      };
      if (name.endsWith('.css') || name === '@/lib/api') return {};
      if (name === './AccountManagement') return { AccountManagement: () => null };
      if (name === './WrikeWorkflow') return { WrikeWorkflow: () => null };
      return require(name);
    },
  });
  const html = renderToStaticMarkup(exports.PersonalWorkspace({ isAdmin: false }));
  const select = html.match(/<select aria-label="Filter applications by stage"[\s\S]*?<\/select>/)?.[0];
  assert.ok(select, 'The stage filter must render');
  return [...select.matchAll(/<option[^>]*>(.*?)<\/option>/g)].map(match => match[1]);
}

const standard = ['All stages', 'Saved', 'Preparing', 'Applied', 'Shortlisted', 'Accepted', 'Unsuccessful', 'Withdrawn'];
assert.deepEqual(stageChoices([]), standard, 'A new account must have stage choices before saving a lead');
const lead = { id: 1, opportunity: { id: 10, title: 'Health grant' }, stage: 'Saved' };
assert.deepEqual(stageChoices([lead]), standard, 'A single saved lead must not hide the other stages');
const wrike = { ...lead, wrike_task_id: 'TASK1', workflow_stage: 'Founder review' };
assert.deepEqual(stageChoices([wrike, { ...wrike, id: 2 }]), [...standard, 'Founder review'],
  'The filter must retain and deduplicate exact Wrike workflow labels');
assert.deepEqual(stageChoices([{ ...lead, wrike_task_id: 'TASK2' }]), [...standard, 'Wrike status not checked'],
  'A linked task that has not been checked must still be filterable');
console.log('Workspace stage dropdown: 4 regression scenarios passed');
