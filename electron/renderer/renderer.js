'use strict';

const api = window.launchpad;
const $ = id => document.getElementById(id);
const state = {diagnostics: null, runtime: {}, schedule: {}, receipts: [], busy: false, filter: 'all', first: true};
const scenarioInfo = {
  future: ['Future warning', '◷', 'Look ahead for evidence-backed risks before their deadline.'],
  decision: ['Decision repair', '↗', 'Turn a concrete blocker into an actionable next decision.'],
  connections: ['Useful connections', '⋈', 'Connect independently observed work when the connection changes the next step.'],
  parallel: ['Parallel experiments', '⫴', 'Compare bounded experiments and report the verified difference.'],
  meeting: ['Meeting preparation', '▱', 'Prepare useful context from explicitly configured meeting evidence.'],
  intentions: ['Dropped intentions', '↺', 'Surface a documented commitment that needs a real next action.'],
  win: ['Verified wins', '✧', 'Bring back a tangible artifact and evidence, not a motivational placeholder.'],
  adversary: ['Adversarial review', '◇', 'Stress-test an explicit plan with a bounded, reproducible experiment.'],
  timeline: ['Causal timeline', '≋', 'Build a causal account from actual events and artifacts.'],
  interrupt: ['Interruption gate', '◎', 'Protect attention with shared evidence, dedupe, quiet hours, and budgets.'],
};
const labels = {queued: 'Queued', sent_unverified: 'Sent · unverified', delivered: 'Delivered · evidence', user_confirmed: 'User-confirmed', unknown: 'Unknown', intent: 'Enqueue intent', dry_run: 'Dry-run', suppressed: 'Suppressed', error: 'Error'};

function node(tag, className, text) {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (text !== undefined && text !== null) result.textContent = String(text);
  return result;
}

function clear(element) { element.replaceChildren(); }
function formatTime(value) {
  if (!value) return 'Not observed';
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? 'Unknown time' : date.toLocaleString(undefined, {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'});
}
function tag(value) { return node('span', 'tag ' + (labels[value] ? value : ''), labels[value] || value); }
function empty(target, title, detail) {
  const box = node('div', 'empty');
  box.append(node('strong', '', title), node('span', '', detail));
  target.append(box);
}
function toast(message) {
  $('toast').textContent = message;
  $('toast').hidden = false;
  setTimeout(() => { $('toast').hidden = true; }, 6500);
}
function fail(message) {
  $('banner').textContent = message;
  $('banner').hidden = false;
}
function tab(name) {
  if (!['mission', 'onboarding', 'scenarios', 'receipts'].includes(name)) return;
  document.querySelectorAll('[data-tab]').forEach(button => {
    const selected = button.dataset.tab === name;
    button.classList.toggle('selected', selected);
    button.setAttribute('aria-selected', String(selected));
    if (selected) $('breadcrumb').textContent = button.textContent.trim();
  });
  document.querySelectorAll('.view').forEach(view => { view.hidden = view.id !== 'view-' + name; });
}

async function operation(label, action) {
  if (state.busy) return null;
  state.busy = true;
  $('operation-text').textContent = label;
  $('operation').hidden = false;
  $('banner').hidden = true;
  document.body.setAttribute('aria-busy', 'true');
  render();
  try {
    const result = await action();
    if (!result?.ok) throw new Error(result?.message || 'The local operation reported an error. Inspect its receipts.');
    if (result.cancelled) return null;
    return result;
  } catch (error) {
    fail(error.message || 'Local operation failed; no successful delivery is assumed.');
    return null;
  } finally {
    state.busy = false;
    $('operation').hidden = true;
    document.body.setAttribute('aria-busy', 'false');
    await refresh(false);
  }
}

async function refresh(showError = true) {
  if (!api) {
    fail('The secured desktop bridge is unavailable. Open this page through the Electron application.');
    return;
  }
  try {
    const result = await api.status();
    state.runtime = result.runtime || {};
    if (!result.ok) {
      state.diagnostics = null;
      if (showError) fail(result.message || 'Runtime diagnostics could not be read.');
    } else {
      state.diagnostics = result.diagnostics;
      state.schedule = result.schedule || {};
      if (result.diagnostics.configured) {
        const receipts = await api.receipts();
        if (receipts.ok) state.receipts = receipts.receipts;
        else if (showError) fail(receipts.message || 'Receipt reconciliation failed; no delivery claim is made.');
      }
    }
    if (state.first && !state.diagnostics?.configured) tab('onboarding');
    state.first = false;
    render();
  } catch {
    if (showError) fail('The desktop bridge could not read local state.');
  }
}

function render() {
  const d = state.diagnostics;
  const configured = Boolean(d?.configured);
  const mac = state.runtime.platform === 'darwin';
  const busy = state.busy;
  $('connection').textContent = !d ? 'Runtime unavailable' : !configured ? 'Setup required' : d.ready_to_queue ? 'Local pipeline connected' : 'Needs attention';
  $('connection').classList.toggle('neutral', !d?.ready_to_queue);
  $('setup-indicator').hidden = configured;
  $('metric-queue').textContent = d?.outbox?.counts?.queued ?? '—';
  $('metric-scenarios').textContent = configured ? (d.scenarios || []).filter(item => item.enabled).length : '—';
  $('metric-budget').textContent = d?.policy ? d.policy.max_daily + ' / day' : '—';
  $('metric-quiet').textContent = d?.policy ? (d.policy.quiet_hours.enabled ? `Quiet ${d.policy.quiet_hours.start}–${d.policy.quiet_hours.end} · ${d.policy.timezone}` : 'Quiet hours explicitly disabled') : 'Policy has not been loaded';
  $('metric-ledger').textContent = d?.ledger ? (d.ledger.ok ? 'Verified' : 'Blocked') : '—';
  $('metric-frames').textContent = d?.ledger?.ok ? `${d.ledger.frames} unsigned local frames checked` : 'No claim without verification';
  $('pipeline-mode').textContent = configured ? (d.transport_mode === 'existing' ? 'Existing service, unchanged' : 'Portable copy of the same core') : 'Not yet configured';
  $('preview-all').disabled = busy || !configured;
  $('queue-all').disabled = busy || !configured || !mac || !d.send_enabled;
  $('self-test').disabled = busy || !configured || !mac;
  $('verify-chain').disabled = busy || !configured;
  $('doctor').disabled = busy;
  $('refresh').disabled = busy;
  $('python-summary').textContent = d?.python ? `Found Python ${d.python.version} · ${d.python.executable}` : 'Python 3.9+ is missing or could not be started.';
  $('setup-form').hidden = configured;
  $('configured-summary').hidden = !configured;
  $('configured-summary').textContent = configured ? `${d.transport_mode === 'existing' ? 'Existing pipeline reused' : 'Portable canonical core configured'}\nRecipient: ${d.outbox?.recipient_masked || 'Unavailable'}\nRuntime: ${d.home}\nSource configuration: private; never included in releases.` : '';
  $('pipeline-guidance').textContent = configured ? 'Bound to one canonical runtime. Existing recipient configuration is read-only here.' :
    d?.existing_available ? 'An existing Storykeeper pipeline was detected. Reuse it: no new sender, permissions context, or duplicate drainer.' :
      'No existing service was detected. New-Mac setup includes the same audited MIT outbox core.';
  const select = $('transport-mode');
  select.options[0].disabled = !d?.existing_available;
  select.options[1].disabled = Boolean(d?.existing_available);
  select.value = d?.existing_available ? 'existing' : 'portable';
  $('recipient-field').hidden = select.value !== 'portable';
  $('configure').disabled = busy || !d || !mac;
  $('state-path').textContent = d?.state_location || d?.config_path || 'Not configured';
  document.querySelectorAll('[data-help="messages"], [data-help="automation"]').forEach(button => { button.disabled = busy || !mac; });
  $('send-enabled').checked = Boolean(d?.send_enabled);
  $('send-enabled').disabled = busy || !configured || !mac;
  $('app-schedule').checked = Boolean(d?.app_schedule);
  $('app-schedule').disabled = busy || !d?.send_enabled || state.schedule.installed || !mac;
  $('login-startup').checked = Boolean(state.runtime.login);
  $('login-startup').disabled = busy || !state.runtime.packaged || !mac;
  $('install-schedule').disabled = busy || !d?.send_enabled || !mac || state.schedule.installed;
  $('remove-schedule').disabled = busy || !state.schedule.owned_by_config || !mac;
  $('schedule-state').textContent = state.schedule.installed ? `${state.schedule.state || 'unknown'} · ${state.schedule.owned_by_config ? 'this configuration' : 'a different configuration'} · ${state.schedule.label}` : 'No independent Launchpad job is installed.';
  $('schedule-badge').textContent = state.schedule.loaded ? 'PERSISTENT JOB LOADED' : state.runtime.schedulerActive ? 'APP SCHEDULE ACTIVE' : 'NO ACTIVE SCHEDULE';
  $('import-sources').disabled = busy || !configured;
  $('sources-summary').textContent = configured ? ` ${d.source_entries || 0} top-level source entries configured.` : '';
  renderPosture();
  renderChecks();
  renderScenarios();
  renderReceipts();
}

function renderPosture() {
  const d = state.diagnostics;
  clear($('posture'));
  const values = [
    ['Transport', d?.configured ? d.transport_mode === 'existing' ? 'Existing canonical service' : 'Portable canonical core' : 'Not configured'],
    ['Message production', d?.send_enabled ? 'Explicitly enabled' : 'Disabled · previews only'],
    ['Automation', 'Unknown until explicit self-test'],
    ['Drainer', d?.drainer?.state || 'Not observed'],
    ['Latest tick', formatTime(d?.last_tick?.finished_at)],
    ['Delivery', 'Requires separate evidence or your confirmation'],
  ];
  for (const [label, value] of values) {
    const row = node('div');
    row.append(node('dt', '', label), node('dd', '', value));
    $('posture').append(row);
  }
}

function renderChecks() {
  clear($('checks'));
  const checks = state.diagnostics?.checks || [{status: 'blocked', title: 'Python runtime unavailable', detail: 'Install Python 3.9+ from python.org, restart Launchpad, and rerun diagnostics.'}];
  for (const check of checks) {
    const row = node('div', 'check ' + (['pass', 'blocked', 'unknown'].includes(check.status) ? check.status : 'unknown'));
    row.append(node('span', 'check-symbol', check.status === 'pass' ? '✓' : check.status === 'blocked' ? '!' : '◌'));
    const content = node('div');
    content.append(node('strong', '', check.title), node('p', '', check.detail));
    row.append(content);
    $('checks').append(row);
  }
}

function renderScenarios() {
  clear($('scenario-grid'));
  const scenarios = state.diagnostics?.scenarios || [];
  if (!scenarios.length) {
    empty($('scenario-grid'), 'Configure the local pipeline first', 'Installed modules will appear here. No findings or demonstrations are fabricated.');
    return;
  }
  for (const scenario of scenarios) {
    const info = scenarioInfo[scenario.name] || [scenario.name, '◇', 'An explicitly allowlisted local evidence producer.'];
    const card = node('article', 'panel scenario-card');
    const header = node('div', 'panel-heading');
    const top = node('div', 'scenario-top');
    top.append(node('span', 'scenario-glyph', info[1]), node('h2', '', info[0]));
    header.append(top, node('span', 'tag', scenario.installed ? 'INSTALLED' : 'NOT INSTALLED'));
    card.append(header, node('p', '', info[2]));
    const actions = node('div', 'scenario-actions');
    const preview = node('button', 'text-button', 'Preview evidence →');
    preview.disabled = state.busy || !scenario.installed;
    preview.addEventListener('click', () => run(scenario.name, false));
    const enabled = node('div', 'scenario-enabled');
    const toggle = node('input');
    toggle.type = 'checkbox';
    toggle.checked = scenario.enabled;
    toggle.disabled = state.busy || !scenario.installed;
    toggle.setAttribute('aria-label', 'Enable ' + info[0] + ' for scheduled runs');
    toggle.addEventListener('change', () => {
      const selected = scenarios.filter(item => item.enabled && item.name !== scenario.name).map(item => item.name);
      if (toggle.checked) selected.push(scenario.name);
      operation('Saving explicit scenario consent…', () => api.settings({enabled_scenarios: selected}));
    });
    enabled.append(toggle, node('span', '', 'Enable for schedule'));
    actions.append(preview, enabled);
    card.append(actions);
    $('scenario-grid').append(card);
  }
}

function renderReceipts() {
  clear($('latest-receipts'));
  clear($('receipts-list'));
  const rows = state.receipts;
  if (!rows.length) empty($('latest-receipts'), 'No decisions recorded yet', 'A preview records a receipt without sending a message.');
  for (const row of rows.slice(0, 4)) {
    const item = node('div', 'compact-row');
    const title = node('div');
    title.append(node('strong', '', row.proposal?.title || row.scenario), node('small', '', `${row.scenario} · ${formatTime(row.at)}`));
    item.append(title, tag(row.state));
    $('latest-receipts').append(item);
  }
  const visible = rows.filter(row => state.filter === 'all' || row.state === state.filter || (state.filter === 'error' && ['unknown', 'intent'].includes(row.state)));
  if (!visible.length) empty($('receipts-list'), 'No receipts in this view', 'Real previews, gate decisions, and transport observations appear here. Nothing is synthesized.');
  for (const row of visible) {
    const item = node('article', 'panel receipt');
    const heading = node('div', 'receipt-heading');
    const title = node('div');
    title.append(node('h2', '', row.proposal?.title || row.scenario), node('div', 'receipt-meta', `${row.scenario} · ${formatTime(row.at)} · ${row.id.slice(0, 8)}`));
    heading.append(title, tag(row.state));
    item.append(heading, node('p', 'receipt-reason', row.reason));
    if (row.proposal) {
      const details = node('details');
      details.append(node('summary', '', 'Inspect evidence and decision'));
      const p = row.proposal;
      const evidence = (p.evidence || []).map(entry => `${entry.source}\n${entry.observation}`).join('\n\n');
      details.append(node('pre', '', `CHANGE\n${p.change}\n\nIMPACT\n${p.impact}\n\nACTION\n${p.action}\n\nDECISION\n${p.decision}\n\nEVIDENCE\n${evidence}\n\nFINGERPRINT\n${p.fingerprint}${p.artifacts?.length ? '\n\nPRIVATE ARTIFACT REFERENCES (not automatically attached)\n' + p.artifacts.join('\n') : ''}${row.message_preview ? '\n\nMESSAGE PREVIEW\n' + row.message_preview : ''}`));
      item.append(details);
    }
    if (['queued', 'sent_unverified', 'unknown'].includes(row.state)) {
      const confirm = node('button', 'text-button', 'I received this on the recipient device →');
      confirm.disabled = state.busy;
      confirm.addEventListener('click', () => operation('Recording your explicit receipt confirmation…', () => api.confirmReceipt(row.id)));
      item.append(confirm);
    }
    $('receipts-list').append(item);
  }
}

async function run(scenario, send) {
  const result = await operation(send ? 'Evaluating configured evidence and applying the shared gate…' : 'Building evidence proposals in bounded dry-run workers…', () => api.run({scenario, send}));
  if (result) {
    tab('receipts');
    toast(send ? 'Run recorded. Queue acceptance is not delivery.' : 'Preview recorded. No message was submitted.');
  }
}

document.querySelectorAll('[data-tab]').forEach(button => button.addEventListener('click', () => tab(button.dataset.tab)));
document.querySelectorAll('[data-go]').forEach(button => button.addEventListener('click', () => tab(button.dataset.go)));
document.querySelectorAll('[data-help]').forEach(button => button.addEventListener('click', () => operation('Opening the selected local help destination…', () => api.openHelp(button.dataset.help))));
document.querySelectorAll('[data-filter]').forEach(button => button.addEventListener('click', () => {
  state.filter = button.dataset.filter;
  document.querySelectorAll('[data-filter]').forEach(item => item.classList.toggle('active', item === button));
  renderReceipts();
}));
$('refresh').addEventListener('click', () => refresh());
$('doctor').addEventListener('click', () => operation('Checking real runtime diagnostics without sending…', () => api.status()));
$('preview-all').addEventListener('click', () => run('all', false));
$('queue-all').addEventListener('click', () => run('all', true));
$('transport-mode').addEventListener('change', () => { $('recipient-field').hidden = $('transport-mode').value !== 'portable'; });
$('configure').addEventListener('click', async () => {
  const mode = $('transport-mode').value;
  const request = {mode};
  if (mode === 'portable') request.recipient = $('recipient').value.trim();
  try {
    const result = await operation('Configuring the private canonical runtime…', () => api.setup(request));
    if (result) toast('Pipeline configured. No message has been sent.');
  } finally {
    $('recipient').value = '';
    delete request.recipient;
  }
});
$('self-test').addEventListener('click', async () => {
  const result = await operation('Requesting one explicit onboarding self-test…', () => api.selfTest());
  if (result) {
    $('test-result').textContent = `Self-test code ${result.code}. Current receipt: ${labels[result.receipt.state] || result.receipt.state}. ${result.receipt.reason} Check the recipient device, then confirm its receipt.`;
    $('test-result').hidden = false;
    toast('Self-test recorded. Receipt remains unverified until there is evidence.');
  }
});
$('send-enabled').addEventListener('change', event => {
  const enabled = event.target.checked;
  operation('Saving explicit send consent…', () => api.settings({send_enabled: enabled, ...(enabled ? {} : {app_schedule: false})}));
});
$('app-schedule').addEventListener('change', event => {
  const enabled = event.target.checked;
  operation('Updating app-only scheduling…', () => api.settings({app_schedule: enabled}));
});
$('login-startup').addEventListener('change', event => {
  const enabled = event.target.checked;
  operation('Updating macOS login startup…', () => api.login(enabled));
});
$('install-schedule').addEventListener('click', () => operation('Installing only the scoped Launchpad schedule…', () => api.schedule({action: 'install'})));
$('remove-schedule').addEventListener('click', () => operation('Removing only the scoped Launchpad schedule…', () => api.schedule({action: 'uninstall'})));
$('verify-chain').addEventListener('click', async () => {
  const result = await operation('Verifying every receipt and its durable head…', () => api.verify());
  if (result) toast(`${result.frames} local frames verified. This does not establish external delivery.`);
});
$('import-sources').addEventListener('click', () => operation('Choosing a private source configuration…', () => api.importSources()));
refresh();
setInterval(() => { if (!state.busy) refresh(false); }, 30_000);
