/* ═══════════════════════════════════════════════════
   HireAI — Frontend Application Logic
   Handles routing, API calls, and dynamic rendering
   ═══════════════════════════════════════════════════ */

'use strict';

// ── State ────────────────────────────────────────────────────────────────────
let allCandidates = [];
let pollingActive = false;
const activeTasks = new Map();

// ── Page Navigation ───────────────────────────────────────────────────────────
document.querySelectorAll('.nav-item').forEach(item => {
  item.addEventListener('click', e => {
    e.preventDefault();
    const page = item.dataset.page;
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    item.classList.add('active');
    document.getElementById('page-' + page).classList.add('active');

    // Lazy-load page data
    if (page === 'dashboard')   loadStats();
    if (page === 'candidates')  loadCandidates();
    if (page === 'learning')    loadLearnings();
    if (page === 'anticheat')   loadFlaggedCandidates();
    if (page === 'architecture')renderArchDiagram();
  });
});

// ── API Helpers ───────────────────────────────────────────────────────────────
async function apiFetch(path, opts = {}) {
  try {
    const res = await fetch('/api' + path, {
      headers: { 'Content-Type': 'application/json' },
      ...opts,
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    data._httpStatus = res.status;
    return data;
  } catch (err) {
    showToast('API error: ' + err.message, 'error');
    throw err;
  }
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function pollTaskUntilDone(taskId, label, options = {}) {
  const timeoutMs = options.timeoutMs || 120000;
  const pollEveryMs = options.pollEveryMs || 2000;
  const start = Date.now();
  activeTasks.set(taskId, { label, startedAt: start });

  while (Date.now() - start < timeoutMs) {
    const task = await apiFetch('/tasks/' + taskId);
    if (task.ready) {
      activeTasks.delete(taskId);
      if (!task.successful) {
        throw new Error((task.result && task.result.error) || `${label} failed`);
      }
      return task.result || {};
    }
    await sleep(pollEveryMs);
  }

  activeTasks.delete(taskId);
  throw new Error(`${label} timed out while waiting for the queue`);
}

async function resolveMaybeQueued(response, label, options = {}) {
  if (response && response.status === 'queued' && response.task_id) {
    showToast(`${label} queued`, 'success');
    return pollTaskUntilDone(response.task_id, label, options);
  }
  return response;
}

function setPollingIndicator(isActive, text) {
  pollingActive = isActive;
  const btn = document.getElementById('pollBtn');
  if (!btn) return;
  btn.textContent = isActive ? 'ON' : 'OFF';
  btn.className = isActive ? 'toggle-btn on' : 'toggle-btn off';
  document.getElementById('statusDot').innerHTML = `<span class="dot${isActive ? ' active' : ''}"></span> ${text}`;
}

// Toast ─────────────────────────────────────────────────────────────────────
function showToast(msg, type = '') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast show ' + type;
  setTimeout(() => el.classList.remove('show'), 3000);
}

// ── Dashboard ─────────────────────────────────────────────────────────────────
async function loadStats() {
  try {
    const s = await apiFetch('/stats');

    // Stat cards
    setText('stat-total',    s.total_candidates ?? 0, '.stat-num');
    setText('stat-fasttrack',(s.tier_breakdown || {})['Fast-Track'] ?? 0, '.stat-num');
    setText('stat-standard', (s.tier_breakdown || {})['Standard'] ?? 0, '.stat-num');
    setText('stat-rejected', (s.tier_breakdown || {})['Reject'] ?? 0, '.stat-num');
    setText('stat-aiflags',  s.ai_flags_total ?? 0, '.stat-num');
    setText('stat-avgscore', (s.average_score ?? 0) + '%', '.stat-num');
    renderDashboardLearningStatus(s.learning_status || {});

    // Tier bars
    renderTierBars(s.tier_breakdown || {}, s.total_candidates || 1);

    // Recent activity from candidates
    loadRecentActivity();
  } catch {}
}

function setText(id, val, sel) {
  const el = document.getElementById(id);
  if (el) (sel ? el.querySelector(sel) : el).textContent = val;
}

function renderTierBars(tiers, total) {
  const colours = {
    'Fast-Track': 'var(--green)',
    'Standard':   'var(--blue)',
    'Review':     'var(--yellow)',
    'Reject':     'var(--red)',
    'Unknown':    'var(--text-dim)',
  };
  const el = document.getElementById('tierBars');
  if (!el) return;
  el.innerHTML = Object.entries(tiers).map(([tier, count]) => {
    const pct = Math.round((count / total) * 100);
    return `
      <div class="tier-bar-row">
        <div class="tier-bar-label">${tier}</div>
        <div class="tier-bar-track">
          <div class="tier-bar-fill" style="width:${pct}%;background:${colours[tier]||'var(--text-dim)'}"></div>
        </div>
        <div class="tier-bar-count">${count}</div>
      </div>`;
  }).join('');
}

async function loadRecentActivity() {
  try {
    const candidates = await apiFetch('/candidates');
    const el = document.getElementById('recentActivity');
    if (!candidates.length) {
      el.innerHTML = '<div class="activity-empty">No activity yet.</div>';
      return;
    }
    // Show last 8 ingested
    const recent = candidates.slice(0, 8);
    el.innerHTML = recent.map(c => {
      const tierClass = 'badge-' + (c.tier === 'Fast-Track' ? 'fast' : (c.tier||'').toLowerCase());
      return `
        <div class="activity-item">
          <span class="activity-badge ${tierClass}">${c.tier||'?'}</span>
          <span class="activity-text">${c.name || 'Unknown'} — ${c.email || ''}</span>
          <span class="activity-time">${c.total_score?.toFixed(1) ?? '—'}</span>
        </div>`;
    }).join('');
  } catch {}
}

// ── Candidates ────────────────────────────────────────────────────────────────
async function loadCandidates() {
  try {
    allCandidates = await apiFetch('/candidates');
    renderCandidateTable(allCandidates);
  } catch {}
}

function renderCandidateTable(rows) {
  const body = document.getElementById('candidateTableBody');
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="7" class="empty-cell">No candidates found.</td></tr>';
    return;
  }
  body.innerHTML = rows.map(c => {
    const score = c.total_score ?? 0;
    const colour = score >= 70 ? 'var(--green)' : score >= 50 ? 'var(--blue)' : score >= 35 ? 'var(--yellow)' : 'var(--red)';
    const strikes = c.total_strikes || 0;
    const strikeClass = strikes === 0 ? 'strikes-0' : strikes === 1 ? 'strikes-1' : strikes === 2 ? 'strikes-2' : 'strikes-3plus';
    const ghCell = c.github_url
      ? `<a href="${c.github_url}" target="_blank" class="gh-link">⎔ GitHub</a>`
      : '<span class="gh-none">—</span>';
    const eliminated = c.is_eliminated ? ' 🚫' : '';
    return `
      <tr>
        <td><strong>${c.name || '—'}${eliminated}</strong><br>
            <small style="color:var(--text-dim);font-family:var(--mono);font-size:11px">${c.email||''}</small></td>
        <td>
          <div class="score-bar-inline">
            <span class="score-val">${score.toFixed(1)}</span>
            <div class="score-track"><div class="score-fill" style="width:${score}%;background:${colour}"></div></div>
          </div>
        </td>
        <td><span class="tier-badge tier-${c.tier||'Unknown'}">${c.tier||'?'}</span></td>
        <td>${ghCell}</td>
        <td><span class="strike-count ${strikeClass}">${strikes} ⚡</span></td>
        <td><span style="font-family:var(--mono);font-size:12px">${c.current_round||0}</span></td>
        <td><button class="btn-detail" onclick="openCandidateModal(${c.id})">View</button></td>
      </tr>`;
  }).join('');
}

function filterCandidates() {
  const search = document.getElementById('searchInput').value.toLowerCase();
  const tier   = document.getElementById('tierFilter').value;
  let rows = allCandidates;
  if (search) rows = rows.filter(c => (c.name||'').toLowerCase().includes(search) || (c.email||'').toLowerCase().includes(search));
  if (tier)   rows = rows.filter(c => c.tier === tier);
  renderCandidateTable(rows);
}

// ── Candidate Modal ───────────────────────────────────────────────────────────
async function openCandidateModal(id) {
  try {
    const c = await apiFetch('/candidates/' + id);
    const modal   = document.getElementById('modal');
    const content = document.getElementById('modalContent');

    const bd = c.score_breakdown || {};
    const answers = c.answers || {};
    const interactions = c.interactions || [];
    const antiCheatLogs = c.anti_cheat_logs || [];
    const reviewNotes = c.review_notes || [];
    const strikeLogs = antiCheatLogs;
    const activeWeights = c.current_active_weights || {};

    content.innerHTML = `
      <div class="modal-name">${c.name || 'Unknown'} ${c.is_eliminated ? '🚫 Eliminated' : ''}</div>
      <div class="modal-email">${c.email || ''} · ${c.college || ''}</div>

      <div class="modal-section">
        <h4>Score Breakdown</h4>
        <div class="score-breakdown-grid">
          ${[['Skills',       bd.skills_score],
             ['Answers',      bd.answer_score],
             ['GitHub',       bd.github_score],
             ['Penalty',      bd.penalty_score],
             ['Completeness', bd.completeness_score],
             ['FINAL',        bd.final_score]
            ].map(([l,v]) => `
            <div class="sbd-item">
              <div class="sbd-label">${l}</div>
              <div class="sbd-val">${v != null ? v.toFixed(1) : '—'}</div>
            </div>`).join('')}
        </div>
      </div>

      <div class="modal-section">
        <h4>Skills</h4>
        <div style="font-family:var(--mono);font-size:12px;color:var(--text-dim)">${c.skills || 'Not provided'}</div>
      </div>

      <div class="modal-section">
        <h4>Current Active Weights</h4>
        <div class="weight-updates">
          ${Object.entries(activeWeights).map(([key, value]) =>
            `<span class="weight-chip">${escapeHtml(key)}: ${(Number(value || 0) * 100).toFixed(0)}%</span>`
          ).join('') || '<span class="activity-empty">No active weights available.</span>'}
        </div>
      </div>

      ${Object.keys(answers).length ? `
      <div class="modal-section">
        <h4>Answers</h4>
        ${Object.entries(answers).map(([q,a]) => `
          <div class="answer-item">
            <div class="answer-q">${q}</div>
            <div class="answer-a">${a || '<em>blank</em>'}</div>
          </div>`).join('')}
      </div>` : ''}

      ${interactions.length ? `
      <div class="modal-section">
        <h4>Email Interactions (${interactions.length})</h4>
        ${interactions.map(i => `
          <div class="interaction-item ${i.direction}">
            <div class="int-meta">${i.direction.toUpperCase()} · Round ${i.round} · ${i.timestamp?.slice(0,16)||''}</div>
            <div class="int-body">${(i.body||'').slice(0,300)}${(i.body||'').length > 300 ? '…' : ''}</div>
          </div>`).join('')}
      </div>` : ''}

      <div class="modal-section">
        <h4>Anti-Cheat Status</h4>
        <div style="font-family:var(--mono);font-size:12px">
          Strikes: <span style="color:${(c.total_strikes||0) > 0 ? 'var(--red)' : 'var(--green)'}">${c.total_strikes||0}</span> ·
          AI Flags: ${c.ai_flag_count||0} ·
          Copy Flags: ${c.copy_flag_count||0}
        </div>
      </div>
      ${reviewNotes.length ? `
      <div class="modal-section">
        <h4>Reply Review</h4>
        ${reviewNotes.map(note => `
          <div class="answer-item">
            <div class="answer-q">${escapeHtml(note.review_type || 'Review')}</div>
            <div class="answer-a">${formatReviewNote(note)}</div>
          </div>`).join('')}
      </div>` : ''}
      ${strikeLogs.length ? `
      <div class="modal-section">
        <h4>Anti-Cheat Events</h4>
        ${strikeLogs.map(log => `
          <div class="answer-item">
            <div class="answer-q">${formatAntiCheatTitle(log)}</div>
            <div class="answer-a">${formatAntiCheatDetails(log)}</div>
          </div>`).join('')}
      </div>` : ''}
    `;

    modal.classList.add('open');
  } catch {}
}

function closeModal(e) {
  if (!e || e.target === document.getElementById('modal')) {
    document.getElementById('modal').classList.remove('open');
  }
}

function formatAntiCheatTitle(log) {
  const details = log.details || '';
  const match = details.match(/Question:\s*([^\n]+)/);
  return match ? `Question: ${escapeHtml(match[1])}` : `Check: ${escapeHtml(log.check_type || 'Anti-cheat')}`;
}

function formatAntiCheatDetails(log) {
  const details = escapeHtml(log.details || '');
  return details.replace(/\n/g, '<br>');
}

function formatReviewNote(note) {
  const score = typeof note.score === 'number' && note.score > 0
    ? `<div style="font-family:var(--mono);font-size:12px;color:var(--text-dim);margin-bottom:6px">Score: ${(note.score * 100).toFixed(1)}%</div>`
    : '';
  const summary = escapeHtml(note.summary || '').replace(/\n/g, '<br>');
  return `${score}${summary}`;
}

async function refreshAfterIngest() {
  await Promise.allSettled([
    loadStats(),
    loadCandidates(),
    loadFlaggedCandidates(),
    loadLearnings(),
  ]);
}

// ── Ingestion ─────────────────────────────────────────────────────────────────
async function ingestDemo() {
  const count   = parseInt(document.getElementById('demoCount').value) || 20;
  const jobRole = document.getElementById('demoRole').value || 'Software Engineer Intern';
  showToast('Ingesting ' + count + ' demo candidates...');
  try {
    const queued = await apiFetch('/ingest/demo', {
      method: 'POST',
      body: JSON.stringify({ count, job_role: jobRole }),
    });
    const r = await resolveMaybeQueued(queued, 'Demo ingestion');
    const el = document.getElementById('ingestResult');
    el.style.display = 'block';
    document.getElementById('ingestResultBody').innerHTML = `
      <div style="font-family:var(--mono);font-size:13px;color:var(--green)">
        Ingested ${r.ingested} / ${r.total} candidates
      </div>
      <div style="margin-top:10px;font-size:12px;color:var(--text-dim)">
        Sample results:<br>
        ${(r.details||[]).map(formatIngestResult).join('<br>')}
      </div>`;
    showToast('Ingestion complete', 'success');
    await refreshAfterIngest();
  } catch {}
}

function formatIngestResult(d) {
  const score = typeof d.score === 'number' ? d.score.toFixed(1) : '?';
  const tier = d.tier || '?';
  const suffix = d.error ? ` — ${escapeHtml(d.error)}` : d.reason ? ` — ${escapeHtml(d.reason)}` : '';
  return `${d.status} — score ${score} (${tier})${suffix}`;
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[ch]));
}

async function handleFileUpload(evt) {
  const file = evt.target.files[0];
  if (!file) return;
  const jobRole = document.getElementById('uploadRole').value || 'Software Engineer Intern';
  document.getElementById('uploadStatus').textContent = 'Uploading...';

  const fd = new FormData();
  fd.append('file', file);
  fd.append('job_role', jobRole);

  try {
    const res = await fetch('/api/ingest/upload', { method: 'POST', body: fd });
    const r = await res.json();
    if (!res.ok) throw new Error(r.error);
    const finalResult = r.status === 'queued' && r.task_id
      ? await pollTaskUntilDone(r.task_id, 'Upload ingestion')
      : r;
    document.getElementById('uploadStatus').textContent = `Ingested ${finalResult.ingested} / ${finalResult.total}`;
    showToast('Upload complete', 'success');
    await refreshAfterIngest();
  } catch (e) {
    document.getElementById('uploadStatus').textContent = 'Error: ' + e.message;
    showToast('Upload failed: ' + e.message, 'error');
  }
}

// Drag-and-drop support
const dropZone = document.getElementById('dropZone');
if (dropZone) {
  dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.style.borderColor = 'var(--amber)'; });
  dropZone.addEventListener('dragleave', () => { dropZone.style.borderColor = ''; });
  dropZone.addEventListener('drop', e => {
    e.preventDefault();
    dropZone.style.borderColor = '';
    const file = e.dataTransfer.files[0];
    if (file) {
      const input = document.getElementById('fileInput');
      const dt = new DataTransfer();
      dt.items.add(file);
      input.files = dt.files;
      handleFileUpload({ target: { files: [file] } });
    }
  });
}

// ── Anti-Cheat Live Detector ──────────────────────────────────────────────────
async function runAntiCheat() {
  const question = document.getElementById('acQuestion').value.trim();
  const answer   = document.getElementById('acAnswer').value.trim();
  const latency  = parseInt(document.getElementById('acLatency').value) || -1;

  if (!question || !answer) {
    showToast('Please enter both question and answer', 'error');
    return;
  }

  showToast('Running analysis…');

  // We call the backend via a dedicated endpoint (inline for demo — uses scoring agent logic)
  try {
    const r = await apiFetch('/anticheat/check', {
      method: 'POST',
      body: JSON.stringify({ question, answer, latency }),
    });

    const score = r.ai_score ?? 0;
    const pct   = (score * 100).toFixed(1);
    const cls   = score >= 0.8 ? 'flagged' : score >= 0.6 ? 'risky' : 'safe';
    const label = score >= 0.8 ? '🚨 FLAGGED' : score >= 0.6 ? '⚠ Suspicious' : '✓ Likely Human';

    document.getElementById('acResult').style.display = 'block';
    document.getElementById('acResultBody').innerHTML = `
      <div class="ac-score-display">
        <div class="ac-score-big ${cls}">${pct}%</div>
        <div>
          <div style="font-size:16px;font-weight:700;color:var(--text-bright)">${label}</div>
          <div style="font-size:12px;color:var(--text-dim);margin-top:4px">${r.ai_explanation || ''}</div>
        </div>
      </div>
      ${r.timing_flagged ? `<div style="color:var(--amber);font-size:13px;margin-bottom:8px">⚡ Fast reply flagged (${latency}s)</div>` : ''}
      ${(r.flags||[]).length ? `
        <div class="ac-flags">
          ${r.flags.map(f => `<span class="ac-flag-chip">${f}</span>`).join('')}
        </div>` : ''}
      <div style="margin-top:12px;font-size:12px;color:var(--text-dim)">
        Strikes awarded this check: <span style="font-family:var(--mono);color:var(--amber)">${r.strikes||0}</span>
      </div>`;
    showToast(label, score >= 0.8 ? 'error' : '');
  } catch {}
}

async function loadFlaggedCandidates() {
  try {
    const rows = await apiFetch('/candidates');
    const flagged = rows.filter(c =>
      (c.total_strikes || 0) > 0 ||
      c.is_eliminated ||
      c.tier === 'Review'
    );
    const el = document.getElementById('flaggedList');
    if (!flagged.length) {
      el.innerHTML = '<div class="activity-empty">No candidates currently need anti-cheat review.</div>';
      return;
    }
    el.innerHTML = flagged.map(c => {
      const reviewReason = c.is_eliminated
        ? 'Eliminated'
        : (c.total_strikes || 0) > 0
          ? `${c.total_strikes} strikes`
          : 'Review tier';
      return `
      <div class="activity-item">
        <span class="activity-badge badge-flag">⚡ ${reviewReason}</span>
        <span class="activity-text">${c.name||'?'} — ${c.email||''}</span>
        <span class="activity-time">${c.tier || 'Unknown'} · ${c.is_eliminated ? 'Eliminated' : 'Needs Review'}</span>
      </div>`;
    }).join('');
  } catch {}
}

// ── Learnings ─────────────────────────────────────────────────────────────────
async function loadLearnings() {
  try {
    const [rows, status] = await Promise.all([
      apiFetch('/learnings'),
      apiFetch('/learning/status'),
    ]);
    const el = document.getElementById('learningsList');
    if (!rows.length) {
      const weightChips = Object.entries(status.current_weights || {}).map(([k,v]) =>
        `<span class="weight-chip">${k}: ${(v*100).toFixed(0)}%</span>`
      ).join('');
      el.innerHTML = `
        <div class="activity-empty">No learnings yet. Run a cycle to generate insights.</div>
        <div class="learning-card">
          <div class="learning-meta">Learning due: ${status.learning_due ? 'Yes' : 'No'} · Candidates: ${status.candidate_count || 0}</div>
          ${weightChips ? `<div class="weight-updates">${weightChips}</div>` : ''}
        </div>`;
      return;
    }
    const statusCard = `
      <div class="learning-card">
        <div class="learning-meta">
          Learning due: ${status.learning_due ? 'Yes' : 'No'} · Candidates: ${status.candidate_count || 0} · Last learning batch: ${status.latest_learning_count || 0}
        </div>
        <div class="weight-updates">
          ${Object.entries(status.current_weights || {}).map(([k,v]) =>
            `<span class="weight-chip">${k}: ${(v*100).toFixed(0)}%</span>`
          ).join('')}
        </div>
      </div>`;
    el.innerHTML = rows.map(r => {
      const weights = r.pattern_updates || {};
      const weightChips = Object.entries(weights).map(([k,v]) =>
        `<span class="weight-chip">${k}: ${(v*100).toFixed(0)}%</span>`
      ).join('');
      return `
        <div class="learning-card">
          <div class="learning-meta">
            Generated ${r.generated_at?.slice(0,16)||'?'} · ${r.candidate_count} candidates analysed
          </div>
          <div class="learning-insights">
            ${(r.insights||[]).map(ins =>
              `<div class="insight-item"><span class="insight-bullet">◆</span> ${ins}</div>`
            ).join('')}
          </div>
          ${weightChips ? `<div class="weight-updates">${weightChips}</div>` : ''}
        </div>`;
    }).join('');
    el.innerHTML = statusCard + el.innerHTML;
  } catch {}
}

function renderDashboardLearningStatus(status) {
  const el = document.getElementById('dashboardLearningStatus');
  if (!el) return;

  const weights = Object.entries(status.current_weights || {}).map(([key, value]) =>
    `<span class="weight-chip">${escapeHtml(key)}: ${(Number(value || 0) * 100).toFixed(0)}%</span>`
  ).join('');

  el.innerHTML = `
    <div class="learning-card">
      <div class="learning-meta">
        Learning due: ${status.learning_due ? 'Yes' : 'No'} · Candidates: ${status.candidate_count ?? 0} · Last learning batch: ${status.latest_learning_count ?? 0}
      </div>
      <div class="learning-meta">
        Last generated: ${status.last_generated_at ? escapeHtml(String(status.last_generated_at).slice(0, 16)) : 'Not yet run'}
      </div>
      ${weights ? `<div class="weight-updates">${weights}</div>` : '<div class="activity-empty">No active weights found yet.</div>'}
    </div>
  `;
}

async function triggerLearning() {
  showToast('Starting learning cycle...');
  try {
    const queued = await apiFetch('/learning/run', { method: 'POST' });
    const result = await resolveMaybeQueued(queued, 'Learning cycle');
    showToast(result.status || 'Learning cycle completed', 'success');
    await loadLearnings();
  } catch {}
}

// ── Email Polling ─────────────────────────────────────────────────────────────
async function togglePolling() {
  try {
    if (!pollingActive) {
      const response = await apiFetch('/polling/start', { method: 'POST' });
      if (response.status === 'queued' && response.task_id) {
        await pollTaskUntilDone(response.task_id, 'Email polling cycle', { timeoutMs: 60000 });
      }
      setPollingIndicator(true, response.beat_enabled ? 'Beat Polling Active' : 'Polling Active');
      showToast(response.message || 'Email polling started', 'success');
    } else {
      const response = await apiFetch('/polling/stop', { method: 'POST' });
      const stillScheduled = response.status === 'scheduled';
      setPollingIndicator(stillScheduled, stillScheduled ? 'Beat Polling Active' : 'System Idle');
      showToast(response.message || 'Email polling stopped');
    }
  } catch {}
}

// ── Architecture Diagram ──────────────────────────────────────────────────────
function renderArchDiagram() {
  const el = document.getElementById('archDiagram');
  if (!el) return;
  el.textContent = `
  +---------------------------------------------------------------+
  |                 AUTONOMOUS HIRING PIPELINE                    |
  +---------------------------------------------------------------+

  [CSV/XLSX Upload]                 [Gmail Inbox + Thread IDs]
         |                                   |
         v                                   v
  +--------------+                  +----------------------+
  | ACCESS       |                  | ENGAGEMENT           |
  | Extractor    |----------------->| Gmail + Recruiter AI |
  +------+-------+                  +----------+-----------+
         |                                     |
         v                                     v
  +--------------+                  +----------------------+
  | INTELLIGENCE |                  | ANTI-CHEAT           |
  | Dynamic Score|                  | AI + timing + copies |
  +------+-------+                  +----------+-----------+
         |                                     |
         +-------------------+-----------------+
                             v
                    +------------------+
                    | SQLite State DB  |
                    | candidates/emails|
                    +---------+--------+
                              |
                              v
                    +------------------+
                    | SELF-LEARNING    |
                    | updates weights  |
                    +---------+--------+
                              |
                              v
                    +------------------+
                    | Celery + Redis   |
                    | worker + Beat    |
                    +------------------+

  Beat keeps polling Gmail and checking learning intervals after restarts.
  Copy-ring checks run on both application answers and email replies.
  Code replies get lightweight syntax/static review before recruiter feedback.
  `;
}

// ── Init ──────────────────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', () => {
  loadStats();
  renderArchDiagram();

  // Auto-refresh stats every 30s
  setInterval(loadStats, 30000);
});
