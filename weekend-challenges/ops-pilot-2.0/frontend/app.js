/* OpsPilot dashboard.
 *
 * Vanilla JS on purpose: the dashboard reads a handful of JSON endpoints and
 * renders them. A framework would add a build step and node_modules to a
 * project whose whole point is that `terraform apply` is the only build step.
 *
 * Everything shown here comes from the API, which reads real state out of
 * DynamoDB, S3 and CloudWatch. Where a value is genuinely unknown the UI says
 * so rather than showing a plausible placeholder.
 */

'use strict';

const CONFIG = window.OPSPILOT_CONFIG || {};
const API = (CONFIG.apiUrl || '').replace(/\/$/, '');
const REFRESH_MS = 15000;

const state = {
  view: 'list',
  incidentId: null,
  incidents: [],
  metrics: null,
  demo: null,
  postmortem: null,
  busy: false,
  timer: null,
};

/* ---------------------------------------------------------------- utilities */

const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'html') node.innerHTML = value;
    else if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2), value);
    } else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
  }
  return node;
}

/** Escape text for the few places that build HTML strings. */
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function fmtTime(iso) {
  if (!iso) return '–';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function fmtDateTime(iso) {
  if (!iso) return '–';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleString([], {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit',
  });
}

function fmtRelative(iso) {
  if (!iso) return '–';
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return String(iso);
  const secs = Math.round((Date.now() - then) / 1000);
  if (secs < 60) return `${Math.max(secs, 0)}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

function pct(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return null;
  return Math.round(n * 100);
}

/* -------------------------------------------------------------------- toast */

function toast(title, message, kind = '') {
  const node = el('div', { class: `toast ${kind}` },
    el('strong', {}, title),
    message ? el('span', {}, message) : null,
  );
  $('toasts').appendChild(node);
  setTimeout(() => {
    node.style.opacity = '0';
    node.style.transition = 'opacity .3s';
    setTimeout(() => node.remove(), 320);
  }, kind === 'err' ? 8000 : 5200);
}

/* ---------------------------------------------------------------------- api */

async function api(path, options = {}) {
  if (!API) throw new Error('API URL is not configured');
  const response = await fetch(`${API}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    throw new Error(`HTTP ${response.status}: response was not JSON`);
  }
  if (!response.ok || payload.ok === false) {
    const err = payload && payload.error ? payload.error : {};
    const error = new Error(err.message || `HTTP ${response.status}`);
    error.code = err.code || String(response.status);
    error.payload = payload;
    throw error;
  }
  return payload.data;
}

/* ------------------------------------------------------------------- header */

function renderHeader() {
  $('env-chip').textContent = `${CONFIG.environment || 'showcase'} · ${CONFIG.region || ''}`;
  $('foot-model').textContent = CONFIG.bedrockModel || '–';
  $('foot-region').textContent = CONFIG.region || '–';

  const pill = $('health-pill');
  const text = $('health-text');
  const m = state.metrics;

  if (!m) {
    pill.className = 'health is-unknown';
    text.textContent = 'Connecting…';
    return;
  }
  if (m.system_healthy) {
    pill.className = 'health is-healthy';
    text.textContent = 'System Healthy';
  } else {
    pill.className = 'health is-degraded';
    const parts = [];
    if (m.active_incidents) parts.push(`${m.active_incidents} active incident${m.active_incidents === 1 ? '' : 's'}`);
    if (m.alarms_firing && m.alarms_firing.length) parts.push(`${m.alarms_firing.length} alarm firing`);
    text.textContent = parts.length ? parts.join(' · ') : 'Degraded';
  }
}

function renderTiles() {
  const m = state.metrics;
  if (!m) return;

  const set = (id, value, alert = false, good = false) => {
    const node = $(id);
    node.textContent = value;
    const tile = node.closest('.tile');
    tile.classList.toggle('is-alert', alert);
    tile.classList.toggle('is-good', good);
  };

  set('m-active', m.active_incidents ?? 0, (m.active_incidents ?? 0) > 0);
  set('m-today', m.incidents_today ?? 0);
  set('m-resolved', m.resolved_today ?? 0, false, (m.resolved_today ?? 0) > 0);
  set('m-remediations', m.auto_remediations ?? 0);

  const mttr = $('m-mttr');
  if (m.average_mttr_minutes === null || m.average_mttr_minutes === undefined) {
    mttr.innerHTML = '–';
  } else {
    mttr.innerHTML = `${esc(m.average_mttr_minutes)}<span class="unit">min</span>`;
  }
}

/* ---------------------------------------------------------------- demo lab */

function renderDemoStatus() {
  const box = $('demo-status');
  const d = state.demo;
  box.innerHTML = '';

  if (!d) {
    box.appendChild(el('span', {}, 'Demo Lab status unavailable.'));
    return;
  }

  box.appendChild(el('span', {},
    'Demo app: ',
    el('code', {}, d.healthy ? 'healthy' : (d.active_scenario || 'degraded')),
  ));

  const firing = d.alarms_firing || [];
  box.appendChild(el('span', {},
    'Alarms firing: ',
    el('code', {}, firing.length ? String(firing.length) : '0'),
  ));

  if (d.active_scenario) {
    box.appendChild(el('span', {}, 'Injected scenario: ', el('code', {}, d.active_scenario)));
  }
  box.appendChild(el('span', {}, 'Checked ', fmtRelative(d.checked_at)));
}

function setDemoBusy(busy) {
  state.busy = busy;
  $('demo-busy').classList.toggle('hidden', !busy);
  document.querySelectorAll('.demo-grid button').forEach((b) => { b.disabled = busy; });
}

async function injectScenario(scenario) {
  setDemoBusy(true);
  try {
    const result = await api('/demo/inject', {
      method: 'POST',
      body: JSON.stringify({ scenario }),
    });
    toast(result.title || 'Failure injected', result.message || '', 'warn');
    await refresh();
  } catch (error) {
    toast('Injection failed', error.message, 'err');
  } finally {
    setDemoBusy(false);
  }
}

async function resetEnvironment() {
  setDemoBusy(true);
  try {
    const result = await api('/demo/reset', { method: 'POST' });
    toast('Environment reset', result.message || 'Demo Lab restored to healthy.', 'ok');
    await refresh();
  } catch (error) {
    toast('Reset failed', error.message, 'err');
  } finally {
    setDemoBusy(false);
  }
}

/* ------------------------------------------------------------ incident list */

function severityBadge(severity) {
  return el('span', { class: `badge sev-${severity || 'UNKNOWN'}` }, severity || 'UNKNOWN');
}

function statusBadge(status) {
  return el('span', { class: `badge st-${status || 'DETECTED'}` },
    String(status || 'DETECTED').replace(/_/g, ' '));
}

function confidenceBar(value) {
  const percent = pct(value);
  if (percent === null) {
    return el('span', { style: 'color:var(--text-dim);font-size:12px;' }, '–');
  }
  const tone = percent >= 75 ? 'high' : percent >= 45 ? 'mid' : 'low';
  return el('span', { class: 'confidence' },
    el('span', { class: 'bar' }, el('span', { class: `fill ${tone}`, style: `width:${percent}%` })),
    el('span', { class: 'pct' }, `${percent}%`),
  );
}

function renderIncidentRows() {
  const body = $('incident-rows');
  body.innerHTML = '';

  if (!state.incidents.length) {
    body.appendChild(el('tr', {}, el('td', { colspan: '7' },
      el('div', { class: 'empty' },
        el('div', { class: 'big' }, '✓'),
        el('div', {}, 'No incidents recorded.'),
        el('div', { style: 'margin-top:6px;font-size:12.5px;' },
          'Use the Demo Lab above to inject a controlled failure.'),
      ),
    )));
    return;
  }

  for (const incident of state.incidents) {
    const row = el('tr', { onclick: () => openIncident(incident.incident_id) },
      el('td', { class: 'id' }, incident.incident_id),
      el('td', {}, severityBadge(incident.severity)),
      el('td', { class: 'nowrap' }, incident.affected_service || '–'),
      el('td', {}, statusBadge(incident.status)),
      el('td', { class: 'nowrap', title: incident.detected_at }, fmtRelative(incident.detected_at)),
      el('td', { class: 'cause' }, incident.root_cause || (
        incident.status === 'DETECTED' || incident.status === 'INVESTIGATING'
          ? 'Investigating…' : 'Not determined'
      )),
      el('td', {}, confidenceBar(incident.confidence)),
    );
    body.appendChild(row);
  }
}

/* ---------------------------------------------------------- incident detail */

function renderTimeline(entries) {
  if (!entries || !entries.length) {
    return el('div', { class: 'notice' }, 'No timeline entries were recorded.');
  }
  const wrap = el('div', { class: 'timeline' });
  for (const entry of entries) {
    wrap.appendChild(el('div', { class: `tl-item kind-${entry.kind || 'opspilot'}` },
      el('span', { class: 'tl-marker' }, entry.icon || '•'),
      el('div', { class: 'tl-time' }, fmtDateTime(entry.timestamp)),
      el('div', { class: 'tl-event' }, entry.event || ''),
      entry.detail ? el('div', { class: 'tl-detail' }, entry.detail) : null,
      entry.source ? el('span', { class: 'tl-source' }, entry.source) : null,
    ));
  }
  return wrap;
}

function renderChanges(changes, window_) {
  if (!changes || !changes.length) {
    return el('div', { class: 'notice' },
      'No infrastructure changes were found in the correlation window.');
  }
  const wrap = el('div', {});
  for (const change of changes.slice(0, 12)) {
    const when = change.minutes_before_incident;
    const timing = when === null || when === undefined
      ? fmtTime(change.timestamp)
      : (when >= 0 ? `${when} min before onset` : `${Math.abs(when)} min after onset`);

    wrap.appendChild(el('div', { class: `change ${change.correlation || 'unrelated'}` },
      el('div', { class: 'change-top' },
        el('span', { class: 'change-action' }, change.action || 'change'),
        el('span', { class: `corr-tag ${change.correlation || 'unrelated'}` },
          String(change.correlation || 'unrelated').replace(/_/g, ' ')),
        el('span', { class: 'change-when' }, timing),
      ),
      el('div', { class: 'change-resource' },
        `${change.service || 'aws'} · ${change.resource || 'unnamed resource'} · by ${change.actor || 'unknown'}`),
      change.correlation_reasons && change.correlation_reasons.length
        ? el('div', { class: 'change-why' }, change.correlation_reasons.join(' · '))
        : null,
      el('span', { class: 'tl-source' }, change.source || 'cloudtrail'),
    ));
  }
  if (window_) {
    wrap.appendChild(el('div', { style: 'margin-top:10px;font-size:11.5px;color:var(--text-dim);' },
      `Correlation window: ${window_.lookback_minutes} minutes before onset.`));
  }
  return wrap;
}

function renderSources(sources) {
  if (!sources || !Object.keys(sources).length) return null;
  const wrap = el('div', {});
  for (const [name, meta] of Object.entries(sources)) {
    if (!meta || typeof meta !== 'object') continue;
    wrap.appendChild(el('div', { class: `src ${meta.available ? 'up' : 'down'}` },
      el('span', { class: 'mark' }, meta.available ? '●' : '○'),
      el('span', { class: 'name' }, name.replace(/_/g, ' ')),
      el('span', { class: 'note' }, meta.note || ''),
    ));
  }
  return wrap;
}

function renderApproval(incident) {
  const executable = (incident.recommendations || []).filter((r) => r.executable);
  const blocked = (incident.recommendations || []).filter((r) => !r.executable);
  const awaiting = incident.status === 'AWAITING_APPROVAL';

  const card = el('div', { class: `card ${awaiting ? 'approval' : ''}` },
    el('h3', {}, 'Recommended Remediation'));

  if (!executable.length && !blocked.length) {
    card.appendChild(el('div', { class: 'notice' }, 'No remediation has been recommended yet.'));
    return card;
  }

  for (const rec of executable) {
    card.appendChild(el('div', { class: 'rec' },
      el('div', { class: 'rec-title' }, rec.title || rec.action),
      el('div', { class: 'rec-key' }, rec.action),
      el('div', { class: 'rec-meta' },
        el('span', {}, `Risk: ${rec.risk || 'UNKNOWN'}`),
        el('span', {}, `Confidence: ${pct(incident.confidence) ?? 0}%`),
        el('span', {}, 'Allowlisted ✓'),
      ),
      rec.reason ? el('div', { class: 'rec-reason' }, rec.reason) : null,
    ));
  }

  for (const rec of blocked) {
    card.appendChild(el('div', { class: 'rec not-executable' },
      el('div', { class: 'rec-title' }, 'Manual remediation required'),
      el('div', { class: 'rec-key' }, `proposed: ${rec.proposed_action || 'unknown'}`),
      el('div', { class: 'rec-meta' },
        el('span', {}, rec.allowlisted ? 'Not applicable to this failure' : 'Not in the remediation allowlist'),
      ),
      rec.reason ? el('div', { class: 'rec-reason' }, rec.reason) : null,
    ));
  }

  if (awaiting && executable.length) {
    const action = executable[0].action;
    card.appendChild(el('div', { class: 'approval-actions' },
      el('button', {
        class: 'primary',
        id: 'btn-approve',
        onclick: () => approve(incident.incident_id, action),
      }, 'Approve Remediation'),
      el('button', {
        class: 'danger',
        onclick: () => reject(incident.incident_id),
      }, 'Reject'),
    ));
    card.appendChild(el('div', { style: 'margin-top:10px;font-size:11.5px;color:var(--text-dim);' },
      'OpsPilot never executes a remediation without this approval, and can only ever '
      + 'run an action from its fixed allowlist against Demo Lab resources.'));
  } else if (awaiting) {
    card.appendChild(el('div', { class: 'notice warn' },
      'Manual remediation required: nothing the analysis proposed maps to an allowlisted action.'));
  }

  return card;
}

function renderVerification(incident) {
  const detail = incident.verification_detail || {};
  const status = incident.verification_status || 'PENDING';
  const card = el('div', { class: 'card' }, el('h3', {}, 'Verification'));

  const toneClass = status === 'VERIFIED' ? 'ok'
    : status === 'VERIFICATION_FAILED' ? 'err' : '';
  card.appendChild(el('div', { class: `notice ${toneClass}` },
    `${status.replace(/_/g, ' ')}${detail.reason ? ` , ${detail.reason}` : ''}`));

  const probes = (detail.checks || []).filter((c) => c.kind !== 'metrics');
  if (probes.length) {
    const list = el('div', { style: 'margin-top:12px;' });
    for (const probe of probes) {
      list.appendChild(el('div', { class: `probe-row ${probe.healthy ? 'good' : 'bad'}` },
        el('span', { class: 't' }, `+${probe.offset_seconds}s`),
        el('span', { class: 'st' }, probe.healthy ? '✓' : '✗'),
        el('span', { class: 'rest' },
          `HTTP ${probe.status_code ?? '–'} · ${probe.duration_ms ?? '–'}ms · alarm ${probe.alarm_state || '–'}`),
      ));
    }
    card.appendChild(list);
  }
  return card;
}

function renderSimilar(incident) {
  const similar = incident.similar_incidents || [];
  const card = el('div', { class: 'card' }, el('h3', {}, 'Similar Past Incidents'));

  if (!similar.length) {
    card.appendChild(el('div', { class: 'notice' },
      'No previous incident shares this failure signature.'));
    return card;
  }
  for (const match of similar) {
    card.appendChild(el('div', {
      class: 'similar',
      onclick: () => openIncident(match.incident_id),
    },
      el('div', { class: 'sid' }, match.incident_id),
      el('div', { class: 'stitle' }, match.title || match.incident_type || 'Incident'),
      el('div', { class: 'smeta' },
        `${fmtDateTime(match.detected_at)} · Resolution: ${match.resolution || 'n/a'} · ${match.outcome || ''}`),
    ));
  }
  card.appendChild(el('div', { style: 'margin-top:10px;font-size:11.5px;color:var(--text-dim);' },
    'Recalled deterministically by failure signature. Past incidents are context, not proof.'));
  return card;
}

/** Minimal Markdown renderer for the postmortem: headings, tables, lists, rules. */
function renderMarkdown(markdown) {
  const lines = String(markdown || '').split('\n');
  const out = [];
  let inList = false;
  let inTable = false;

  const closeList = () => { if (inList) { out.push('</ul>'); inList = false; } };
  const closeTable = () => { if (inTable) { out.push('</tbody></table>'); inTable = false; } };

  const inline = (text) => esc(text)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*]+)\*/g, '$1<em>$2</em>')
    .replace(/\\\|/g, '|');

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    const trimmed = line.trim();

    if (!trimmed) { closeList(); closeTable(); continue; }

    if (/^\|\s*-{2,}/.test(trimmed)) continue; // table separator row

    if (trimmed.startsWith('|')) {
      const cells = trimmed.replace(/^\||\|$/g, '').split(/(?<!\\)\|/).map((c) => c.trim());
      if (!inTable) {
        closeList();
        out.push('<table><thead><tr>');
        cells.forEach((c) => out.push(`<th>${inline(c)}</th>`));
        out.push('</tr></thead><tbody>');
        inTable = true;
      } else {
        out.push('<tr>');
        cells.forEach((c) => out.push(`<td>${inline(c)}</td>`));
        out.push('</tr>');
      }
      continue;
    }
    closeTable();

    if (trimmed.startsWith('### ')) { closeList(); out.push(`<h3>${inline(trimmed.slice(4))}</h3>`); continue; }
    if (trimmed.startsWith('## ')) { closeList(); out.push(`<h2>${inline(trimmed.slice(3))}</h2>`); continue; }
    if (trimmed.startsWith('# ')) { closeList(); out.push(`<h1>${inline(trimmed.slice(2))}</h1>`); continue; }
    if (trimmed === '---') { closeList(); out.push('<hr>'); continue; }
    if (trimmed.startsWith('> ')) { closeList(); out.push(`<blockquote>${inline(trimmed.slice(2))}</blockquote>`); continue; }

    if (trimmed.startsWith('- ')) {
      if (!inList) { out.push('<ul>'); inList = true; }
      out.push(`<li>${inline(trimmed.slice(2))}</li>`);
      continue;
    }
    closeList();
    out.push(`<p>${inline(trimmed)}</p>`);
  }
  closeList();
  closeTable();
  return out.join('');
}

function renderPostmortemCard(incident) {
  const card = el('div', { class: 'card' }, el('h3', {}, 'Postmortem'));

  if (!incident.postmortem_key) {
    card.appendChild(el('div', { class: 'notice' },
      'A postmortem is generated automatically once the incident reaches a terminal state.'));
    return card;
  }

  if (state.postmortem && state.postmortem.incident_id === incident.incident_id) {
    card.appendChild(el('div', {
      class: 'postmortem',
      html: renderMarkdown(state.postmortem.markdown),
    }));
    card.appendChild(el('div', { style: 'margin-top:10px;font-size:11.5px;color:var(--text-dim);' },
      `Stored at ${incident.postmortem_location || 'S3'} · narrative: `
      + `${incident.postmortem_narrative_source || 'unknown'} · all facts are read from the incident record.`));
  } else {
    card.appendChild(el('button', {
      onclick: () => loadPostmortem(incident.incident_id),
    }, 'Load postmortem'));
  }
  return card;
}

function renderDetail(incident) {
  const view = $('view-detail');
  view.innerHTML = '';

  const rootCause = incident.root_cause || {};
  const remediation = incident.remediation_detail || {};

  view.appendChild(el('div', { class: 'detail-head' },
    el('div', { class: 'titles' },
      el('h2', {}, incident.title || incident.incident_id),
      el('div', { class: 'sub' },
        el('span', {}, incident.incident_id),
        el('span', {}, incident.affected_service || ''),
        el('span', {}, `detected ${fmtDateTime(incident.detected_at)}`),
      ),
    ),
    el('div', { class: 'actions' },
      severityBadge(incident.severity),
      statusBadge(incident.status),
      el('button', { class: 'ghost', onclick: () => reinvestigate(incident.incident_id) }, 'Re-investigate'),
      el('button', { class: 'ghost', onclick: showList }, '← All incidents'),
    ),
  ));

  const left = el('div', {});
  const right = el('div', {});

  /* --- Summary --- */
  const summary = el('div', { class: 'card' },
    el('h3', {}, 'Summary'),
    el('p', {}, incident.ai_summary || incident.description || 'No summary is available yet.'),
  );
  if (incident.ai_status && incident.ai_status !== 'OK') {
    summary.appendChild(el('div', { class: 'notice warn' },
      incident.ai_status === 'UNAVAILABLE'
        ? 'AI investigation unavailable , this incident was analysed from deterministic evidence only.'
        : 'The model response could not be used; OpsPilot fell back to deterministic analysis.'
      + (incident.fallback_reason ? ` (${incident.fallback_reason})` : '')));
  }
  left.appendChild(summary);

  /* --- Timeline: the key visual --- */
  left.appendChild(el('div', { class: 'card' },
    el('h3', {}, 'Investigation Timeline'),
    renderTimeline(incident.timeline),
  ));

  /* --- Changes --- */
  left.appendChild(el('div', { class: 'card' },
    el('h3', {}, 'Infrastructure Changes'),
    incident.change_summary
      ? el('p', { style: 'color:var(--text-muted);font-size:13px;' }, incident.change_summary)
      : null,
    renderChanges(incident.changes, incident.change_window),
  ));

  /* --- Postmortem --- */
  left.appendChild(renderPostmortemCard(incident));

  /* --- Root cause --- */
  const causeCard = el('div', { class: 'card' },
    el('h3', {}, 'Root Cause'),
    el('p', {}, rootCause.description || 'Root cause has not been determined.'),
    el('dl', { class: 'kv' },
      el('dt', {}, 'Confidence'), el('dd', {}, confidenceBar(rootCause.confidence)),
      el('dt', {}, 'Category'), el('dd', {}, rootCause.category || 'unknown'),
      el('dt', {}, 'Analysis'), el('dd', {}, incident.ai_status === 'OK'
        ? `Bedrock (${CONFIG.bedrockModel || 'configured model'})`
        : 'Deterministic fallback'),
    ),
  );
  right.appendChild(causeCard);

  /* --- Evidence --- */
  const evidence = incident.evidence || [];
  const evidenceCard = el('div', { class: 'card' }, el('h3', {}, 'Evidence'));
  if (evidence.length) {
    evidenceCard.appendChild(el('ul', { class: 'plain' }, evidence.map((e) => el('li', {}, e))));
  } else {
    evidenceCard.appendChild(el('div', { class: 'notice' }, 'No evidence has been recorded yet.'));
  }
  const factors = incident.contributing_factors || [];
  if (factors.length) {
    evidenceCard.appendChild(el('h3', { style: 'margin-top:16px;' }, 'Contributing Factors'));
    evidenceCard.appendChild(el('ul', { class: 'plain' }, factors.map((f) => el('li', {}, f))));
  }
  const sources = renderSources(incident.evidence_sources);
  if (sources) {
    evidenceCard.appendChild(el('h3', { style: 'margin-top:16px;' }, 'Evidence Sources'));
    evidenceCard.appendChild(sources);
  }
  right.appendChild(evidenceCard);

  /* --- Approval --- */
  right.appendChild(renderApproval(incident));

  /* --- Remediation status --- */
  right.appendChild(el('div', { class: 'card' },
    el('h3', {}, 'Remediation Status'),
    el('dl', { class: 'kv' },
      el('dt', {}, 'State'), el('dd', {}, (incident.remediation_status || 'NOT_STARTED').replace(/_/g, ' ')),
      el('dt', {}, 'Action'), el('dd', {}, incident.approved_action || '–'),
      el('dt', {}, 'Approved by'), el('dd', {}, incident.approved_by || '–'),
      el('dt', {}, 'Target'), el('dd', {}, remediation.target || '–'),
      el('dt', {}, 'Completed'), el('dd', {}, remediation.completed_at ? fmtDateTime(remediation.completed_at) : '–'),
    ),
    remediation.error ? el('div', { class: 'notice err', style: 'margin-top:10px;' }, remediation.error) : null,
  ));

  /* --- Verification --- */
  right.appendChild(renderVerification(incident));

  /* --- Similar --- */
  right.appendChild(renderSimilar(incident));

  view.appendChild(el('div', { class: 'detail-grid' }, left, right));
}

/* -------------------------------------------------------------- navigation */

function showList() {
  state.view = 'list';
  state.incidentId = null;
  state.postmortem = null;
  $('view-list').classList.remove('hidden');
  $('view-detail').classList.add('hidden');
  if (window.location.hash) window.location.hash = '';
  refresh();
}

async function openIncident(incidentId) {
  state.view = 'detail';
  state.incidentId = incidentId;
  state.postmortem = null;
  $('view-list').classList.add('hidden');
  $('view-detail').classList.remove('hidden');
  $('view-detail').innerHTML = '<div class="empty"><span class="spinner"></span> Loading incident…</div>';
  window.location.hash = incidentId;
  await loadIncident();
}

async function loadIncident() {
  if (!state.incidentId) return;
  try {
    const incident = await api(`/incidents/${encodeURIComponent(state.incidentId)}`);
    if (state.view === 'detail' && state.incidentId === incident.incident_id) {
      renderDetail(incident);
    }
  } catch (error) {
    $('view-detail').innerHTML = '';
    $('view-detail').appendChild(el('div', { class: 'card' },
      el('div', { class: 'notice err' }, `Could not load incident: ${error.message}`),
      el('div', { style: 'margin-top:12px;' },
        el('button', { class: 'ghost', onclick: showList }, '← All incidents')),
    ));
  }
}

async function loadPostmortem(incidentId) {
  try {
    state.postmortem = await api(`/incidents/${encodeURIComponent(incidentId)}/postmortem`);
    await loadIncident();
  } catch (error) {
    toast('Postmortem unavailable', error.message, 'err');
  }
}

/* ------------------------------------------------------------------ actions */

async function approve(incidentId, action) {
  const button = $('btn-approve');
  if (button) { button.disabled = true; button.textContent = 'Approving…'; }
  try {
    const result = await api(`/incidents/${encodeURIComponent(incidentId)}/approve`, {
      method: 'POST',
      body: JSON.stringify({ action, approved_by: 'dashboard-operator' }),
    });
    toast('Remediation approved', result.message || '', 'ok');
    await loadIncident();
  } catch (error) {
    toast('Approval refused', error.message, 'err');
    if (button) { button.disabled = false; button.textContent = 'Approve Remediation'; }
  }
}

async function reject(incidentId) {
  try {
    const result = await api(`/incidents/${encodeURIComponent(incidentId)}/reject`, {
      method: 'POST',
      body: JSON.stringify({ rejected_by: 'dashboard-operator', reason: 'Rejected from dashboard' }),
    });
    toast('Remediation rejected', result.message || '', 'warn');
    await loadIncident();
  } catch (error) {
    toast('Could not reject', error.message, 'err');
  }
}

async function reinvestigate(incidentId) {
  try {
    await api(`/incidents/${encodeURIComponent(incidentId)}/reinvestigate`, { method: 'POST' });
    toast('Re-investigation dispatched', 'OpsPilot is collecting evidence again.', 'ok');
    setTimeout(loadIncident, 3000);
  } catch (error) {
    toast('Could not re-investigate', error.message, 'err');
  }
}

/* ------------------------------------------------------------------ refresh */

async function refresh() {
  const results = await Promise.allSettled([
    api('/metrics/summary'),
    api('/incidents?limit=50'),
    api('/demo/status'),
  ]);

  const [metrics, incidents, demo] = results;

  if (metrics.status === 'fulfilled') state.metrics = metrics.value;
  if (incidents.status === 'fulfilled') state.incidents = incidents.value || [];
  if (demo.status === 'fulfilled') state.demo = demo.value;

  const failed = results.filter((r) => r.status === 'rejected');
  if (failed.length === results.length) {
    $('health-pill').className = 'health is-degraded';
    $('health-text').textContent = 'API unreachable';
    return;
  }

  renderHeader();
  renderTiles();
  renderDemoStatus();
  if (state.view === 'list') renderIncidentRows();
}

async function tick() {
  try {
    if (state.view === 'detail') {
      await Promise.all([loadIncident(), refresh()]);
    } else {
      await refresh();
    }
  } catch {
    /* transient failures are surfaced by the health pill, not as toasts */
  }
}

/* --------------------------------------------------------------------- init */

function init() {
  if (!API) {
    document.body.innerHTML = '<div class="empty" style="padding:80px 20px;">'
      + 'OpsPilot configuration is missing. config.js is generated by Terraform at apply time.'
      + '</div>';
    return;
  }

  document.querySelectorAll('.demo-grid button.inject').forEach((button) => {
    button.addEventListener('click', () => injectScenario(button.dataset.scenario));
  });
  $('btn-reset').addEventListener('click', resetEnvironment);
  $('btn-refresh').addEventListener('click', () => {
    $('btn-refresh').disabled = true;
    tick().finally(() => { $('btn-refresh').disabled = false; });
  });

  window.addEventListener('hashchange', () => {
    const id = window.location.hash.slice(1);
    if (id && id !== state.incidentId) openIncident(id);
    else if (!id && state.view === 'detail') showList();
  });

  const initial = window.location.hash.slice(1);
  if (initial) openIncident(initial);

  refresh();
  state.timer = setInterval(tick, REFRESH_MS);
}

document.addEventListener('DOMContentLoaded', init);
