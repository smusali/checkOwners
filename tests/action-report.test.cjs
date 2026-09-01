const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const report = require('../.github/scripts/report.cjs');

function setup(t, {fork = false, denied = false, failed = false, existing = false, clean = false} = {}) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'checkowners-report-'));
  t.after(() => fs.rmSync(directory, {recursive: true, force: true}));
  if (!failed) fs.writeFileSync(path.join(directory, 'drift.json'), JSON.stringify({
    drift_detected: !clean, missing: [{path: 'src/a.py'}], max_confidence_delta: 0.5,
  }));
  const calls = {api: [], warnings: [], notices: [], summary: [], writes: 0};
  const summary = {};
  for (const method of ['addHeading', 'addRaw', 'addCodeBlock']) {
    summary[method] = text => {calls.summary.push(text); return summary;};
  }
  summary.write = async () => {calls.writes++;};
  const issues = {};
  for (const method of ['listComments', 'createComment', 'updateComment']) {
    issues[method] = async args => {
      calls.api.push({method, args});
      if (denied) throw new Error('403');
      return {data: existing ? [{id: 1, body: '<!-- checkowners-drift-report -->'}] : []};
    };
  }
  return {calls, args: {
    directory, driftOutcome: failed ? 'failure' : 'success', commentEnabled: true,
    github: {rest: {issues}},
    core: {summary, warning: text => calls.warnings.push(text), notice: text => calls.notices.push(text)},
    context: {eventName: 'pull_request', repo: {owner: 'owner', repo: 'repo'}, issue: {number: 42},
      payload: {pull_request: {head: {repo: {full_name: fork ? 'fork/repo' : 'owner/repo'}}}}},
  }};
}

test('fork gets a complete summary and notice without an API call', async t => {
  const {calls, args} = setup(t, {fork: true});
  await report(args);
  assert.equal(calls.writes, 1);
  assert.ok(calls.summary.some(text => text.includes('src/a.py')));
  assert.equal(calls.api.length, 0);
  assert.equal(calls.notices.length, 1);
});

test('read-only token warns without failing or losing the summary', async t => {
  const {calls, args} = setup(t, {denied: true});
  await report(args);
  assert.equal(calls.writes, 1);
  assert.match(calls.warnings[0], /pull-requests: write.*comment_on_pr: false/);
});

test('analysis failure produces a diagnostic summary and no comment', async t => {
  const {calls, args} = setup(t, {failed: true});
  await report(args);
  assert.equal(calls.writes, 1);
  assert.ok(calls.summary.some(text => text.includes('not a clean ownership result')));
  assert.equal(calls.api.length, 0);
});

test('creates a concise comment for same-repository drift', async t => {
  const {calls, args} = setup(t);
  await report(args);
  assert.equal(calls.api[1].method, 'createComment');
  assert.match(calls.api[1].args.body, /Missing: \*\*1\*\*/);
});

test('updates an existing comment when drift is resolved', async t => {
  const {calls, args} = setup(t, {existing: true, clean: true});
  await report(args);
  assert.equal(calls.api[1].method, 'updateComment');
  assert.match(calls.api[1].args.body, /no drift detected/);
});

test('disabled comments still produce the summary', async t => {
  const {calls, args} = setup(t);
  await report({...args, commentEnabled: false});
  assert.equal(calls.writes, 1);
  assert.equal(calls.api.length, 0);
});

test('malformed report warns and does not claim success', async t => {
  const {calls, args} = setup(t);
  fs.writeFileSync(path.join(args.directory, 'drift.json'), '{');
  await report(args);
  assert.equal(calls.writes, 1);
  assert.equal(calls.api.length, 0);
  assert.equal(calls.warnings.length, 2);
});
