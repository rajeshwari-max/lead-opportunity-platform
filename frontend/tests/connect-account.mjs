import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';

const compiled = ts.transpileModule(
  readFileSync(new URL('../src/lib/connectAccount.ts', import.meta.url), 'utf8'),
  { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS } },
).outputText;

function setup(responses, popup = true) {
  const calls = [];
  const tab = {
    closed: false, document: { title: '', body: {style:{}} },
    close() { this.closed = true; },
    location: { href: 'about:blank', replace(url) { this.href = url; } },
  };
  const exports = {};
  vm.runInNewContext(compiled, {
    exports, Error, setTimeout: cb => { queueMicrotask(cb); },
    window: { open: () => popup ? tab : null },
    fetch: async (url, options) => {
      calls.push([url, options?.method || 'GET']);
      const next = responses.shift();
      assert.ok(next, `Unexpected request: ${url}`);
      if (next.closed) tab.closed = true;
      return { ok: next.ok ?? true, json: async () => next.body };
    },
  });
  return { connect: exports.connectAccount, calls, tab };
}

{
  const s = setup([{body:{remote:false}}, {body:{status:'saved'}}]);
  await s.connect('developmentaid', '/api/devaid/connect');
  assert.equal(s.tab.closed, true);
  assert.equal(s.calls[1][0], '/api/devaid/connect');
}
{
  const s = setup([{body:{remote:true}}, {body:{id:'123', viewer_url:'/viewer'}},
    {body:{state:'ready'}}, {body:{state:'saving'}}, {body:{state:'connected'}}]);
  await s.connect('developmentaid', '/api/devaid/connect');
  assert.equal(s.tab.location.href, '/viewer');
  assert.equal(s.tab.opener, null);
  assert.equal(s.calls.length, 5);
}
{
  const s = setup([{body:{remote:true}}, {ok:false, body:{detail:'A scrape is running.'}}]);
  await assert.rejects(s.connect('developmentaid', '/api/devaid/connect'), /scrape is running/);
  assert.equal(s.tab.closed, false);
  assert.match(s.tab.document.body.textContent, /A scrape is running/);
  assert.match(s.tab.document.title, /Could not open/);
}
{
  const s = setup([{body:{remote:true}},
    {ok:false,body:{detail:'The DevelopmentAid profile is locked. Close its existing Chrome window first.'}}]);
  await assert.rejects(s.connect('developmentaid', '/api/devaid/connect'), /profile is locked/);
  assert.equal(s.tab.closed, false);
  assert.match(s.tab.document.body.textContent, /Close its existing Chrome window/);
}
{
  const s = setup([{ok:false,body:{detail:'Not authenticated'}}]);
  await assert.rejects(s.connect('developmentaid', '/api/devaid/connect'), /Not authenticated/);
  assert.equal(s.tab.closed, false);
  assert.match(s.tab.document.body.textContent, /Not authenticated/);
}
{
  const s = setup([{body:{remote:true}}], false);
  await assert.rejects(s.connect('developmentaid', '/api/devaid/connect'), /Allow popups/);
  assert.equal(s.calls.length, 1);
}
{
  const s = setup([{body:{remote:true}}, {body:{id:'123', viewer_url:'/viewer'}},
    {body:{state:'ready'}, closed:true}, {body:{state:'cancelled'}}]);
  await assert.rejects(s.connect('developmentaid', '/api/devaid/connect'), /closed before saving/);
  assert.equal(s.calls.at(-1)[0], '/api/remote-login/123/cancel');
}
{
  const s = setup([{body:{remote:true}}, {body:{id:'123', viewer_url:'/viewer'}},
    {body:{state:'unverified',message:'Sign-in could not be confirmed.'}}]);
  await assert.rejects(s.connect('developmentaid', '/api/devaid/connect'), /could not be confirmed/);
  assert.equal(s.tab.closed, false);
}
console.log('Connect account: local login, remote save, busy server, popup blocking, cancellation and unverified login passed');
