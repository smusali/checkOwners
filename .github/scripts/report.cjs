const fs = require('node:fs');
const path = require('node:path');

const MARKER = '<!-- checkowners-drift-report -->';

module.exports = async ({github, context, core, directory, driftOutcome, commentEnabled}) => {
  const reports = {};
  core.summary.addHeading('checkOwners');
  for (const [key, filename] of Object.entries({
    drift: 'drift.json', bus: 'bus_factor.json', decay: 'decay.json',
  })) {
    const filenamePath = path.join(directory, filename);
    if (!fs.existsSync(filenamePath)) continue;
    try {
      reports[key] = JSON.parse(fs.readFileSync(filenamePath, 'utf8'));
      core.summary.addHeading(filename, 3).addCodeBlock(JSON.stringify(reports[key], null, 2), 'json');
    } catch {
      core.warning(`checkOwners could not read ${filename}; inspect the analysis step logs.`);
    }
  }
  if (driftOutcome !== 'success' || !reports.drift) {
    core.summary.addRaw('\nDrift analysis did not complete successfully. Inspect the Run checkowners drift step logs; this is not a clean ownership result.\n');
    core.warning('checkOwners drift analysis failed or produced no report. Inspect the Run checkowners drift step logs.');
  }
  await core.summary.write();

  if (!commentEnabled || context.eventName !== 'pull_request') return;
  const headRepo = context.payload.pull_request?.head?.repo?.full_name;
  if (headRepo !== `${context.repo.owner}/${context.repo.repo}`) {
    core.notice('Skipping checkOwners PR comment for a fork pull request. The report is available in the job summary.');
    return;
  }
  if (driftOutcome !== 'success' || !reports.drift) return;

  try {
    const drift = reports.drift;
    const target = {owner: context.repo.owner, repo: context.repo.repo};
    const {data: comments} = await github.rest.issues.listComments({
      ...target, issue_number: context.issue.number, per_page: 100,
    });
    const existing = comments.find(comment => comment.body?.includes(MARKER));
    if (!drift.drift_detected && !existing) return;

    const count = key => (drift[key] || []).length;
    const body = drift.drift_detected ? [
      MARKER,
      '### checkOwners: ownership drift detected',
      '',
      `Missing: **${count('missing')}** · Changed: **${count('changed')}** · Stale: **${count('stale')}**`,
      `Maximum confidence delta: **${Number(drift.max_confidence_delta || 0).toFixed(2)}**`,
      `Critical paths: **${(reports.bus?.critical_paths || []).length}** · Decay warnings: **${(reports.decay?.reports || []).length}**`,
      '',
      'Review the full report in the job summary. Run `checkowners drift` locally to inspect the affected paths, then review `checkowners sync` changes before committing.',
    ].join('\n') : `${MARKER}\n### checkOwners: no drift detected\n\nPreviously reported drift has been resolved.`;
    if (existing) {
      await github.rest.issues.updateComment({...target, comment_id: existing.id, body});
    } else {
      await github.rest.issues.createComment({...target, issue_number: context.issue.number, body});
    }
  } catch {
    core.warning('checkOwners could not update the PR comment. For same-repository pull requests, grant pull-requests: write or supply github_token with comment permission. Set comment_on_pr: false to disable comments. The report remains available in the job summary.');
  }
};
