<script>
  import { onMount } from 'svelte';

  let state = $state({
    server: { ok: false, url: '', message: 'loading' },
    recentWorkspaces: [],
    tasks: [],
    sessions: [],
    sessionsByDirectory: {},
    archivedSessions: [],
    sessionStatus: {},
    metrics: { running: 0, waiting: 0, failed: 0, completedToday: 0 }
  });

  let loading = $state(true);
  let error = $state('');
  let dispatchError = $state('');
  let lastRefreshed = $state(0);
  let now = $state(Date.now());
  let selectedTaskId = $state(null);
  let targetMode = $state('workspace');
  let cwd = $state('/Users/beta/Projects/agent-deck');
  let _autoSessionPicked = false;
  let selectedSessionId = $state('');
  let selectedProjectDir = $state('');
  let mode = $state('normal');
  let prompt = $state('Fix SSE reconnect after network drop');
  let panelTab = $state('task');
  let harnessEditorMode = $state('guided');
  let specFormat = $state('yaml');
  let taskSpec = $state('');
  let harnessError = $state('');
  let folderPickerOpen = $state(false);
  let folderPickerTarget = $state('dispatch');
  let folderBrowsePath = $state('');
  let folderBrowseParent = $state(null);
  let folderBrowseChildren = $state([]);
  let folderPickerLoading = $state(false);
  let folderPickerError = $state('');

  const harnessTemplates = {
    reliability: {
      name: 'Fix SSE reconnect',
      goal: 'Fix SSE reconnect after network drop.',
      steps: [
        'Investigate current SSE implementation',
        'Implement reconnect with backoff',
        'Add regression coverage'
      ],
      acceptance: [
        'SSE reconnects automatically after network drop',
        'Existing tests pass'
      ],
      checkInterval: 5
    },
    review: {
      name: 'Code review pass',
      goal: 'Review recent changes for correctness, security, and test coverage.',
      steps: [
        'Inspect the diff and identify risk areas',
        'Run tests and note failures',
        'Propose or apply focused fixes'
      ],
      acceptance: [
        'No critical issues remain open',
        'Tests pass or failures are documented'
      ],
      checkInterval: 5
    },
    refactor: {
      name: 'Targeted refactor',
      goal: 'Refactor the selected module without changing external behavior.',
      steps: [
        'Map current behavior and dependencies',
        'Apply incremental refactor with small commits',
        'Verify behavior with existing tests'
      ],
      acceptance: [
        'Public API unchanged',
        'All existing tests pass'
      ],
      checkInterval: 10
    }
  };

  const intervalPresets = [1, 5, 15, 30];

  let harnessForm = $state({
    name: harnessTemplates.reliability.name,
    workspace: '/Users/beta/Projects/agent-deck',
    mode: 'normal',
    checkInterval: 5,
    goal: harnessTemplates.reliability.goal,
    steps: [...harnessTemplates.reliability.steps],
    acceptance: [...harnessTemplates.reliability.acceptance]
  });

  let collapsedDirs = $state(new Set());
  let collapsedDirsInitialized = false;
  $effect(() => {
    if (collapsedDirsInitialized) return;
    if (sortedDirectories.length === 0) return;
    collapsedDirsInitialized = true;
    collapsedDirs = new Set(sortedDirectories);
  });

  let drawerSessionId = $state('');
  let drawerTab = $state('messages');
  let drawerLoading = $state(false);
  let drawerError = $state('');
  let drawerMessages = $state([]);
  let drawerDiff = $state([]);
  let drawerLoadedAt = $state(0);

  let archivedPopoverOpen = $state(false);

  let confirmDialog = $state(null);
  function askConfirm({ title, message, confirmLabel = 'Confirm', danger = false }) {
    return new Promise((resolve) => {
      confirmDialog = { title, message, confirmLabel, danger, resolve };
    });
  }
  function resolveConfirm(result) {
    if (confirmDialog?.resolve) confirmDialog.resolve(result);
    confirmDialog = null;
  }

  const sortedDirectories = $derived(
    Object.keys(state.sessionsByDirectory).filter((dir) => dir !== '(unknown)').concat(
      Object.keys(state.sessionsByDirectory).includes('(unknown)') ? ['(unknown)'] : []
    )
  );

  const drawerSession = $derived(
    state.sessions.find((session) => session.id === drawerSessionId) || null
  );

  const archivedRecords = $derived(
    (state.archivedSessions || [])
      .map((a) => ({ id: a.id, title: a.title || a.id.slice(0, 16), directory: a.directory || '' }))
      .sort((a, b) => a.title.toLowerCase().localeCompare(b.title.toLowerCase()))
  );

  const drawerSessionArchived = $derived(
    drawerSession
      ? state.archivedSessions?.some((a) => a.id === drawerSession.id)
      : false
  );

  const refreshAgeSeconds = $derived(
    lastRefreshed ? Math.max(0, Math.floor((now - lastRefreshed) / 1000)) : null
  );

  const sessionsInSelectedProject = $derived(
    selectedProjectDir ? state.sessionsByDirectory[selectedProjectDir] || [] : []
  );

  $effect(() => {
    if (_autoSessionPicked) return;
    if (selectedSessionId) return;
    if (state.sessions.length === 0) return;
    _autoSessionPicked = true;
    const first = state.sessions[0];
    selectedProjectDir = first.directory || '(unknown)';
    selectedSessionId = first.id;
  });

  $effect(() => {
    if (sortedDirectories.length === 0) return;
    if (!selectedProjectDir || !state.sessionsByDirectory[selectedProjectDir]) {
      selectedProjectDir = sortedDirectories[0];
    }
  });

  $effect(() => {
    const sessions = sessionsInSelectedProject;
    if (!sessions.length) return;
    if (!selectedSessionId || !sessions.some((session) => session.id === selectedSessionId)) {
      selectedSessionId = sessions[0].id;
    }
  });

  const activeHarnessTasks = $derived(
    (state.tasks || []).filter((task) => task.status !== 'archived')
  );

  const archivedHarnessTasks = $derived(
    (state.tasks || []).filter((task) => task.status === 'archived')
  );

  const selectedTask = $derived(
    state.tasks.find((task) => task.id === selectedTaskId) ||
    activeHarnessTasks[0] ||
    state.tasks[0] ||
    null
  );

  const apiPayload = $derived(
    targetMode === 'workspace'
      ? {
          target: { type: 'workspace', cwd },
          agent: 'opencode',
          mode,
          prompt
        }
      : {
          target: { type: 'session', sessionId: selectedSessionId },
          agent: 'opencode',
          mode,
          prompt
        }
  );

  const harnessSpecPreview = $derived(
    harnessEditorMode === 'guided' ? specFromHarnessForm() : taskSpec
  );

  const apiPreview = $derived(
    panelTab === 'task'
      ? `POST /api/dispatch\n${JSON.stringify(apiPayload, null, 2)}`
      : `POST /api/tasks\n${JSON.stringify({ format: specFormat, spec: harnessSpecPreview }, null, 2)}`
  );

  function statusClass(status) {
    const value = String(status || '').toLowerCase();
    if (value.includes('archived') || value.includes('deleted') || value.includes('paused')) return 'pill-src';
    if (value === 'retry' || value.includes('wait') || value.includes('permission')) return 'pill-wait';
    if (value.includes('fail') || value.includes('error')) return 'pill-fail';
    if (value === 'idle' || value.includes('complete')) return 'pill-idle';
    if (value === 'busy' || value.includes('pending') || value.includes('running') || value.includes('streaming')) {
      return 'pill-run';
    }
    return 'pill-idle';
  }

  function statusShowsDot(status) {
    const value = String(status || '').toLowerCase();
    return value === 'busy' || value === 'retry' || value.includes('running') || value.includes('wait');
  }

  function stepProgressLabel(task) {
    const total = task?.spec?.steps?.length || 0;
    if (!total) {
      return task?.status === 'completed' ? 'Done' : '—';
    }
    const completed = Array.isArray(task?.completed_steps) ? task.completed_steps.length : 0;
    const done =
      task?.status === 'completed'
        ? total
        : Math.min(completed, total);
    return `${done}/${total}`;
  }

  const harnessPreview = $derived({
    steps: harnessForm.steps.filter((s) => s.trim()).length,
    acceptance: harnessForm.acceptance.filter((s) => s.trim()).length,
    interval: harnessForm.checkInterval
  });

  function yamlQuote(value) {
    if (!value) return '""';
    if (value.includes('\n')) {
      return `|\n${value.split('\n').map((line) => `  ${line}`).join('\n')}`;
    }
    if (value.includes(' ') || value.includes(':') || value.includes('#')) {
      return `"${value.replaceAll('\\', '\\\\').replaceAll('"', '\\"')}"`;
    }
    return value;
  }

  function buildYamlFromForm(form) {
    const lines = [
      `name: ${yamlQuote(form.name.trim())}`,
      `workspace: ${yamlQuote(form.workspace.trim())}`,
      `mode: ${form.mode}`,
      `check_interval_minutes: ${Math.max(1, Number(form.checkInterval) || 5)}`,
      `goal: ${yamlQuote(form.goal.trim())}`,
      'acceptance:'
    ];
    for (const item of form.acceptance) {
      const trimmed = item.trim();
      if (trimmed) lines.push(`  - ${yamlQuote(trimmed)}`);
    }
    lines.push('steps:');
    for (const item of form.steps) {
      const trimmed = item.trim();
      if (trimmed) lines.push(`  - ${yamlQuote(trimmed)}`);
    }
    return `${lines.join('\n')}\n`;
  }

  function buildMarkdownFromForm(form) {
    const lines = [
      `# ${form.name.trim()}`,
      '',
      `workspace: ${form.workspace.trim()}`,
      `mode: ${form.mode}`,
      `check_interval: ${Math.max(1, Number(form.checkInterval) || 5)}m`,
      '',
      '## Goal',
      form.goal.trim(),
      '',
      '## Acceptance'
    ];
    for (const item of form.acceptance) {
      const trimmed = item.trim();
      if (trimmed) lines.push(`- [ ] ${trimmed}`);
    }
    lines.push('', '## Steps');
    form.steps.forEach((item, index) => {
      const trimmed = item.trim();
      if (trimmed) lines.push(`${index + 1}. ${trimmed}`);
    });
    return `${lines.join('\n')}\n`;
  }

  function specFromHarnessForm() {
    return specFormat === 'markdown'
      ? buildMarkdownFromForm(harnessForm)
      : buildYamlFromForm(harnessForm);
  }

  function applyHarnessTemplate(key) {
    const template = harnessTemplates[key];
    if (!template) return;
    harnessForm = {
      ...harnessForm,
      name: template.name,
      goal: template.goal,
      steps: [...template.steps],
      acceptance: [...template.acceptance],
      checkInterval: template.checkInterval
    };
  }

  function loadHarnessExample() {
    taskSpec = buildYamlFromForm(harnessForm);
    specFormat = 'yaml';
  }

  function switchHarnessEditor(mode) {
    if (mode === 'raw' && harnessEditorMode === 'guided') {
      taskSpec = specFromHarnessForm();
    }
    harnessEditorMode = mode;
  }

  function addHarnessStep() {
    harnessForm = { ...harnessForm, steps: [...harnessForm.steps, ''] };
  }

  function removeHarnessStep(index) {
    harnessForm = {
      ...harnessForm,
      steps: harnessForm.steps.filter((_, i) => i !== index)
    };
  }

  function addHarnessAcceptance() {
    harnessForm = { ...harnessForm, acceptance: [...harnessForm.acceptance, ''] };
  }

  function removeHarnessAcceptance(index) {
    harnessForm = {
      ...harnessForm,
      acceptance: harnessForm.acceptance.filter((_, i) => i !== index)
    };
  }

  function sessionStatus(session) {
    if (!session?.id) return 'unknown';
    const status = state.sessionStatus?.[session.id];
    if (typeof status === 'string') return status;
    if (status && typeof status === 'object') {
      return status.type || status.status || status.state || 'idle';
    }
    return 'idle';
  }

  function directoryStatus(sessions) {
    let hasRetry = false;
    for (const session of sessions) {
      const status = String(sessionStatus(session)).toLowerCase();
      if (status === 'busy' || status.includes('running') || status.includes('streaming')) {
        return 'busy';
      }
      if (status === 'retry' || status.includes('wait') || status.includes('permission')) {
        hasRetry = true;
      }
    }
    return hasRetry ? 'retry' : 'idle';
  }

  function updatedAgeSeconds(item) {
    const ts = item?.last_check_at ?? item?.updated ?? item?.updated_at ?? item?.created ?? item?.at;
    if (!ts) return null;
    const seconds = Math.max(0, Math.floor(Date.now() / 1000 - Number(ts)));
    if (seconds < 60) return `${seconds}s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
    return `${Math.floor(seconds / 86400)}d`;
  }

  function toggleDir(path) {
    const next = new Set(collapsedDirs);
    if (next.has(path)) next.delete(path);
    else next.add(path);
    collapsedDirs = next;
  }

  async function openSessionDrawer(sessionId) {
    drawerSessionId = sessionId;
    drawerTab = 'messages';
    drawerError = '';
    drawerMessages = [];
    drawerDiff = [];
    drawerLoading = true;
    await loadDrawerData(sessionId);
  }

  async function loadDrawerData(sessionId) {
    drawerLoading = true;
    drawerError = '';
    try {
      const [msgRes, diffRes] = await Promise.all([
        fetch(`/api/sessions/${sessionId}/messages`),
        fetch(`/api/sessions/${sessionId}/diff`)
      ]);
      if (!msgRes.ok) throw new Error(await msgRes.text());
      const msgBody = await msgRes.json();
      drawerMessages = msgBody.messages || [];
      if (diffRes.ok) {
        const diffBody = await diffRes.json();
        drawerDiff = diffBody.diff || [];
      }
      drawerLoadedAt = Date.now();
    } catch (err) {
      drawerError = err instanceof Error ? err.message : String(err);
    } finally {
      drawerLoading = false;
    }
  }

  async function refreshDrawer() {
    if (!drawerSessionId) return;
    await loadDrawerData(drawerSessionId);
  }

  function closeDrawer() {
    drawerSessionId = '';
  }

  function prepareDispatchToSession(sessionId) {
    if (!sessionId) return;
    const session = state.sessions.find((item) => item.id === sessionId);
    targetMode = 'session';
    selectedProjectDir = session?.directory || '(unknown)';
    selectedSessionId = sessionId;
    panelTab = 'task';
    closeDrawer();
  }

  function selectProjectDir(dir) {
    selectedProjectDir = dir;
    const sessions = state.sessionsByDirectory[dir] || [];
    selectedSessionId = sessions[0]?.id || '';
  }

  async function archiveSessionFromDrawer() {
    if (!drawerSessionId) return;
    const sessionId = drawerSessionId;
    try {
      const res = await fetch(`/api/sessions/${sessionId}/archive`, { method: 'POST' });
      if (!res.ok) throw new Error(await extractError(res));
      closeDrawer();
      await refresh();
    } catch (err) {
      drawerError = err instanceof Error ? err.message : String(err);
    }
  }

  async function unarchiveSessionFromPopover(sessionId) {
    try {
      const res = await fetch(`/api/sessions/${sessionId}/archive`, { method: 'DELETE' });
      if (!res.ok) throw new Error(await extractError(res));
      await refresh();
    } catch (err) {
      error = err instanceof Error ? err.message : String(err);
    }
  }

  async function archiveSessionInline(sessionId) {
    try {
      const res = await fetch(`/api/sessions/${sessionId}/archive`, { method: 'POST' });
      if (!res.ok) throw new Error(extractError(res));
      await refresh();
    } catch (err) {
      error = err instanceof Error ? err.message : String(err);
    }
  }

  async function deleteSessionHard(sessionId) {
    const ok = await askConfirm({
      title: 'Delete session',
      message: 'This will permanently delete the session and all its data. This cannot be undone.',
      confirmLabel: 'Delete',
      danger: true,
    });
    if (!ok) return;
    try {
      const res = await fetch(`/api/sessions/${sessionId}/delete`, { method: 'POST' });
      if (!res.ok) throw new Error(await extractError(res));
      await refresh();
    } catch (err) {
      error = err instanceof Error ? err.message : String(err);
    }
  }

  function messageRole(message) {
    const info = message?.info;
    if (info && typeof info === 'object' && info.role) return String(info.role).toLowerCase();
    return (message?.role || message?.type || 'message').toLowerCase();
  }

  // Read a value from a nested object via a list of candidate keys.
  function pickFirst(obj, keys, fallback = '') {
    if (!obj || typeof obj !== 'object') return fallback;
    for (const key of keys) {
      const v = obj[key];
      if (v !== undefined && v !== null && v !== '') return v;
    }
    return fallback;
  }

  function getMessageInfo(message) {
    if (message?.info && typeof message.info === 'object') return message.info;
    if (message?.metadata && typeof message.metadata === 'object') return message.metadata;
    return null;
  }

  function getMessageParts(message) {
    if (Array.isArray(message?.parts)) return message.parts;
    if (Array.isArray(message?.content)) return message.content;
    return [];
  }

  // Render a single part into a short, human-readable line. Mirrors the
  // backend ``_summarize_part`` so the dashboard does not have to round-trip
  // the raw JSON blob.
  function renderPart(part) {
    if (part == null) return null;
    if (typeof part === 'string') {
      const t = part.trim();
      return t ? t : null;
    }
    if (typeof part !== 'object') return String(part);
    const type = String(part.type || '').toLowerCase();
    if (type === 'text' || type === '') {
      const t = part.text || part.content;
      if (typeof t === 'string' && t.trim()) return t.trim();
      return null;
    }
    if (type === 'reasoning') {
      const t = part.text || part.content || '';
      const cleaned = typeof t === 'string' ? t.trim() : '';
      return cleaned ? `💭 ${cleaned.slice(0, 200)}` : null;
    }
    if (type === 'tool' || type === 'tool_use' || type === 'tool-invocation') {
      const name = part.name || part.tool || 'tool';
      const args = part.input || part.args || part.arguments;
      if (args && typeof args === 'object') {
        for (const key of ['command', 'filePath', 'path', 'file', 'url', 'query', 'prompt']) {
          const v = args[key];
          if (typeof v === 'string' && v.trim()) {
            return `🔧 ${name} — ${v.slice(0, 120)}`;
          }
        }
        const summary = JSON.stringify(args);
        if (summary && summary !== '{}') {
          return `🔧 ${name} — ${summary.slice(0, 120)}`;
        }
      }
      return `🔧 ${name}`;
    }
    if (type === 'tool_result' || type === 'tool-result') {
      const name = part.name || part.tool || 'tool';
      const output = part.output ?? part.content;
      if (typeof output === 'string') {
        const firstLine = output.split('\n').map((line) => line.trim()).find(Boolean);
        return `↪ ${name}${firstLine ? `: ${firstLine.slice(0, 140)}` : ''}`;
      }
      return `↪ ${name}`;
    }
    if (type === 'step-start' || type === 'step-finish') return null;
    // Unknown part type — show a one-line digest of its keys instead of the
    // full JSON dump.
    const keys = Object.keys(part).filter((k) => k !== 'type');
    if (keys.length === 0) return null;
    return `· ${type || 'part'} (${keys.slice(0, 3).join(', ')})`;
  }

  // Build the list of displayable lines for a single message. Empty
  // lines are dropped; if the message has no displayable content at all
  // we return a single placeholder so the user can still see the row.
  function summarizeMessage(message) {
    if (!message) return ['(empty)'];
    if (typeof message.text === 'string' && message.text.trim()) {
      return [message.text.trim()];
    }
    const parts = getMessageParts(message);
    if (Array.isArray(parts) && parts.length > 0) {
      const lines = parts.map(renderPart).filter(Boolean);
      if (lines.length === 0) return ['(no displayable content)'];
      if (lines.length > 5) {
        return [...lines.slice(0, 5), `(+${lines.length - 5} more parts)`];
      }
      return lines;
    }
    // Last resort: digest the message itself instead of dumping JSON.
    const info = getMessageInfo(message);
    if (info) {
      const role = pickFirst(info, ['role']);
      const summary = pickFirst(info, ['summary', 'text', 'content']);
      if (summary) return [String(summary).slice(0, 240)];
      if (role) return [`(${role})`];
    }
    return ['(empty)'];
  }

  function messageTimestamp(message) {
    const info = getMessageInfo(message);
    const t = pickFirst(info, ['createdAt', 'created_at', 'time_created']);
    if (typeof t === 'number' && t > 0) {
      const ms = t > 1e12 ? t : t * 1000;
      return new Date(ms).toLocaleTimeString();
    }
    return null;
  }

  function formatTimestamp(value) {
    if (value === undefined || value === null || value === '') return '—';
    if (typeof value === 'number' && value > 0) {
      const ms = value > 1e12 ? value : value * 1000;
      const d = new Date(ms);
      if (Number.isNaN(d.getTime())) return String(value);
      return d.toLocaleString();
    }
    return String(value);
  }

  function getTimeBlock(session) {
    if (session?.time && typeof session.time === 'object') return session.time;
    if (session?.timestamps && typeof session.timestamps === 'object') return session.timestamps;
    return {};
  }

  function metaFields(session) {
    if (!session) return {};
    const t = getTimeBlock(session);
    const fields = {
      ID: session.id || '—',
      Title: session.title || '—',
      Slug: session.slug || '—',
      Directory: session.directory || session.cwd || '—',
      Parent: session.parentID || session.parentId || '— (root)',
      Agent: session.agent || '—',
      Mode: session.mode || '—',
      Status: sessionStatus(session) || '—',
      Created: formatTimestamp(t.created ?? session.created ?? session.createdAt),
      Updated: formatTimestamp(t.updated ?? session.updated ?? session.updatedAt),
      Archived: t.archived ? formatTimestamp(t.archived) : 'no',
    };
    // Token / cost summary when the session exposes them.
    if (session.tokens && typeof session.tokens === 'object') {
      const tok = session.tokens;
      const parts = [];
      for (const key of ['input', 'output', 'reasoning', 'cacheRead', 'cacheWrite']) {
        if (typeof tok[key] === 'number') parts.push(`${key} ${tok[key].toLocaleString()}`);
      }
      if (parts.length) fields.Tokens = parts.join(' · ');
    }
    if (typeof session.cost === 'number') fields.Cost = `$${session.cost.toFixed(4)}`;
    return fields;
  }

  async function refresh() {
    try {
      error = '';
      const response = await fetch('/api/state');
      if (!response.ok) throw new Error(await extractError(response));
      state = await response.json();
      const tasks = state.tasks || [];
      const active = tasks.filter((task) => task.status !== 'archived');
      if (
        !selectedTaskId ||
        !tasks.some((task) => task.id === selectedTaskId)
      ) {
        selectedTaskId = (active[0] || tasks[0])?.id || null;
      }
      if (drawerSessionId && !state.sessions.some((s) => s.id === drawerSessionId)) {
        drawerSessionId = '';
      }
      lastRefreshed = Date.now();
    } catch (err) {
      error = err instanceof Error ? err.message : String(err);
    } finally {
      loading = false;
    }
  }

  async function dispatchMessage() {
    dispatchError = '';
    try {
      const response = await fetch('/api/dispatch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(apiPayload)
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail || response.statusText);
      if (body.sessionId) drawerSessionId = body.sessionId;
      await refresh();
    } catch (err) {
      dispatchError = err instanceof Error ? err.message : String(err);
    }
  }

  async function createHarnessTask() {
    harnessError = '';
    const spec = harnessEditorMode === 'guided' ? specFromHarnessForm() : taskSpec;
    if (!harnessForm.name.trim() && harnessEditorMode === 'guided') {
      harnessError = 'Task name is required';
      return;
    }
    if (!harnessForm.workspace.trim() && harnessEditorMode === 'guided') {
      harnessError = 'Workspace is required';
      return;
    }
    try {
      const response = await fetch('/api/tasks', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ format: specFormat, spec })
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail || response.statusText);
      selectedTaskId = body.taskId;
      panelTab = 'harness';
      await refresh();
    } catch (err) {
      harnessError = err instanceof Error ? err.message : String(err);
    }
  }

  async function taskAction(action) {
    if (!selectedTask?.id) return;
    try {
      const response = await fetch(`/api/tasks/${selectedTask.id}/${action}`, { method: 'POST' });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail || response.statusText);
      await refresh();
    } catch (err) {
      error = err instanceof Error ? err.message : String(err);
    }
  }

  function selectHarnessTask(taskId) {
    selectedTaskId = taskId;
    panelTab = 'harness';
  }

  function viewHarnessSession(task) {
    const sessionId = task?.active_session_id;
    if (!sessionId) return;
    selectHarnessTask(task.id);
    openSessionDrawer(sessionId);
  }

  async function copyApi() {
    await navigator.clipboard.writeText(apiPreview);
  }

  function shortPath(path) {
    if (!path) return '';
    if (path === '/') return '/';
    return path.replace(/\/$/, '');
  }

  function compactPath(path) {
    const normalized = shortPath(path);
    if (!normalized) return '';
    const parts = normalized.split('/').filter(Boolean);
    if (parts.length <= 2) return normalized.startsWith('/') ? `/${parts.join('/')}` : parts.join('/');
    return `…/${parts.slice(-2).join('/')}`;
  }

  function projectName(path) {
    if (!path || path === '(unknown)') return '(unknown)';
    const parts = shortPath(path).split('/').filter(Boolean);
    return parts[parts.length - 1] || path;
  }

  function projectDirLabel(dir) {
    const count = state.sessionsByDirectory[dir]?.length ?? 0;
    if (dir === '(unknown)') return `Unknown (${count})`;
    const name = projectName(dir);
    const path = compactPath(dir);
    if (path && path !== name) return `${name} — ${path} (${count})`;
    return `${name} (${count})`;
  }

  function sessionOptionLabel(session) {
    const status = sessionStatus(session);
    const age = updatedAgeSeconds(session);
    const bits = [session.title || session.id];
    if (status !== 'idle') bits.push(status);
    if (age) bits.push(age);
    return bits.join(' · ');
  }

  function selectRecentWorkspace(path) {
    cwd = path;
    harnessForm = { ...harnessForm, workspace: path };
  }

  async function removeRecentWorkspace(path, event) {
    event?.stopPropagation?.();
    event?.preventDefault?.();
    try {
      const response = await fetch(`/api/recent-workspaces?path=${encodeURIComponent(path)}`, {
        method: 'DELETE'
      });
      if (!response.ok) throw new Error(await extractError(response));
      await refresh();
    } catch (err) {
      error = err instanceof Error ? err.message : String(err);
    }
  }

  function applyPickedWorkspace(path) {
    if (folderPickerTarget === 'harness') {
      harnessForm = { ...harnessForm, workspace: path };
    } else {
      cwd = path;
    }
    selectRecentWorkspace(path);
  }

  async function loadBrowse(path) {
    folderPickerLoading = true;
    folderPickerError = '';
    try {
      const query = path ? `?path=${encodeURIComponent(path)}` : '';
      const response = await fetch(`/api/browse${query}`);
      if (!response.ok) throw new Error(await extractError(response));
      const body = await response.json();
      folderBrowsePath = body.path;
      folderBrowseParent = body.parent || null;
      folderBrowseChildren = body.children || [];
    } catch (err) {
      folderPickerError = err instanceof Error ? err.message : String(err);
    } finally {
      folderPickerLoading = false;
    }
  }

  async function openFolderBrowser(target, startPath) {
    folderPickerTarget = target;
    folderPickerOpen = true;
    folderPickerError = '';
    await loadBrowse(startPath || (target === 'harness' ? harnessForm.workspace : cwd));
  }

  function closeFolderPicker() {
    folderPickerOpen = false;
    folderPickerError = '';
  }

  function confirmFolderPick() {
    if (!folderBrowsePath) return;
    applyPickedWorkspace(folderBrowsePath);
    closeFolderPicker();
  }

  async function pickWorkspace(target) {
    const initial = target === 'harness' ? harnessForm.workspace : cwd;
    await openFolderBrowser(target, initial);
  }

  function extractError(res) {
    return res.text().then((text) => {
      try {
        const data = JSON.parse(text);
        if (data && typeof data.detail === 'string') return data.detail;
        if (data && data.data && typeof data.data.message === 'string') return data.data.message;
      } catch {
        // not JSON
      }
      return text || `HTTP ${res.status}`;
    });
  }

  function refreshPollDelayMs() {
    const statuses = Object.values(state.sessionStatus || {});
    const hasActive = statuses.some((status) => {
      const value = String(typeof status === 'string' ? status : status?.type || status?.status || '').toLowerCase();
      return value === 'busy' || value === 'retry' || value.includes('running') || value.includes('wait');
    });
    // SSE covers real-time status changes, so the slow /api/state poll
    // only needs to catch new sessions and task updates. Slow it down to
    // avoid hammering the upstream OpenCode server.
    return hasActive ? 5000 : 15000;
  }

  onMount(() => {
    refresh();
    let pollTimer;
    const scheduleRefresh = () => {
      pollTimer = setTimeout(async () => {
        await refresh();
        scheduleRefresh();
      }, refreshPollDelayMs());
    };
    scheduleRefresh();
    const tick = setInterval(() => (now = Date.now()), 1000);
    return () => {
      clearTimeout(pollTimer);
      clearInterval(tick);
    };
  });
</script>

<div class="app">
  <aside class="sidebar">
    <div class="brand">
      <div class="brand-mark">⌘</div>
      <span class="brand-name">OpenDeck</span>
    </div>
    <div class="conn">
      <span class:off={!state.server.ok} class="conn-dot"></span>
      {state.server.ok ? 'Online' : 'Offline'}
    </div>

    <div class="sidebar-section">
      <div class="nav-label">Status</div>
      <div class="status-line"><span class="dim">Server</span><span class="mono" title={state.server.url}>{state.server.url.replace(/^https?:\/\//, '')}</span></div>
      <div class="status-line"><span class="dim">Sessions</span><span class="mono">{state.sessions.length} visible</span></div>
      <button class="status-line status-button" type="button" onclick={() => (archivedPopoverOpen = !archivedPopoverOpen)} aria-expanded={archivedPopoverOpen}>
        <span class="dim">Archived</span>
        <span class="mono">{(state.archivedSessions || []).length} ▸</span>
      </button>
      {#if archivedPopoverOpen}
        <div class="hidden-popover" role="dialog" aria-label="Archived sessions">
          <div class="hidden-popover-head">
            <span>Archived sessions</span>
            <button class="btn btn-ghost btn-sm" type="button" onclick={() => (archivedPopoverOpen = false)}>Close</button>
          </div>
          {#if archivedRecords.length === 0}
            <div class="dim empty-mini">No archived sessions.</div>
          {:else}
            <ul class="hidden-list">
              {#each archivedRecords as record}
                <li>
                  <div class="hidden-title">{record.title}</div>
                  <div class="mono hidden-id">{record.directory || record.id}</div>
                  <div class="row-actions">
                    <button class="btn btn-ghost btn-sm" type="button" onclick={() => unarchiveSessionFromPopover(record.id)}>Unarchive</button>
                    <button class="btn btn-ghost btn-sm btn-danger" type="button" onclick={() => deleteSessionHard(record.id)}>Delete</button>
                  </div>
                </li>
              {/each}
            </ul>
          {/if}
        </div>
      {/if}
      <div class="status-line"><span class="dim">Updated</span><span class="mono">{refreshAgeSeconds === null ? '—' : `${refreshAgeSeconds}s ago`}</span></div>
    </div>

    <div class="sidebar-section sidebar-recent">
      <div class="nav-label">Recent Workspaces</div>
      <div class="recent-list">
        {#if state.recentWorkspaces.length === 0}
          <div class="dim empty-mini">Used paths will appear here.</div>
        {:else}
          {#each state.recentWorkspaces as workspace}
            <div
              class="recent-row"
              class:active={cwd === workspace || harnessForm.workspace === workspace}
            >
              <button
                class="recent-item mono"
                type="button"
                title={workspace}
                onclick={() => selectRecentWorkspace(workspace)}
              >{compactPath(workspace)}</button>
              <button
                class="recent-remove"
                type="button"
                aria-label="Remove from recents"
                title="Remove"
                onclick={(event) => removeRecentWorkspace(workspace, event)}
              >×</button>
            </div>
          {/each}
        {/if}
      </div>
    </div>
  </aside>

  <main class="main">
    <header class="status-bar">
      <div class="status-left">
        <h1>OpenCode Server</h1>
        <span class={`pill ${state.server.ok ? 'pill-ok' : 'pill-fail'}`}>
          <span class="pill-dot"></span>{state.server.ok ? 'Healthy' : 'Unavailable'}
        </span>
        <div class="status-meta">
          <span>{state.sessions.length} sessions</span>
          <span>·</span>
          <span>{state.tasks.length} tasks</span>
        </div>
      </div>
    </header>

    {#if error}
      <div class="error" style="padding: 10px 20px;">{error}</div>
    {/if}

    <section class="table-section">
      {#if loading}
        <div class="empty">Loading OpenDeck state…</div>
      {:else if state.tasks.length === 0 && state.sessions.length === 0}
        <div class="empty">No sessions or harness tasks. Use the Task panel to dispatch or start a harness task.</div>
      {:else}
        {#if activeHarnessTasks.length > 0}
          <div class="group">
            <div class="group-head"><span class="group-title">Harness Tasks</span><span class="group-count">{activeHarnessTasks.length}</span></div>
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Workspace</th>
                  <th>Status</th>
                  <th>Steps</th>
                  <th>Checked</th>
                </tr>
              </thead>
              <tbody>
                {#each activeHarnessTasks as task}
                  <tr class:highlight={selectedTask?.id === task.id} onclick={() => selectHarnessTask(task.id)}>
                    <td><span class="task-title">{task.name}</span></td>
                    <td class="mono">{shortPath(task.workspace)}</td>
                    <td><span class={`pill ${statusClass(task.status)}`}><span class="pill-dot"></span>{task.status}</span></td>
                    <td class="mono">{stepProgressLabel(task)}</td>
                    <td class="mono">{updatedAgeSeconds(task) ?? '—'}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {/if}

        {#if archivedHarnessTasks.length > 0}
          <div class="group">
            <div class="group-head"><span class="group-title">Archived Harness</span><span class="group-count">{archivedHarnessTasks.length}</span></div>
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Workspace</th>
                  <th>Status</th>
                  <th>Steps</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {#each archivedHarnessTasks as task}
                  <tr class:highlight={selectedTask?.id === task.id} onclick={() => selectHarnessTask(task.id)}>
                    <td><span class="task-title">{task.name}</span></td>
                    <td class="mono">{shortPath(task.workspace)}</td>
                    <td><span class={`pill ${statusClass(task.status)}`}><span class="pill-dot"></span>{task.status}</span></td>
                    <td class="mono">{stepProgressLabel(task)}</td>
                    <td>
                      {#if task.active_session_id}
                        <button class="btn btn-ghost btn-sm row-btn" type="button" onclick={(e) => { e.stopPropagation(); viewHarnessSession(task); }}>View</button>
                      {/if}
                    </td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {/if}

        {#if sortedDirectories.length > 0}
          <div class="group">
            <div class="group-head">
              <span class="group-title">Sessions</span>
              <span class="group-count">{state.sessions.length} across {sortedDirectories.length} dir{sortedDirectories.length === 1 ? '' : 's'}</span>
            </div>
            {#each sortedDirectories as dir}
              {@const sessions = state.sessionsByDirectory[dir]}
              {@const isCollapsed = collapsedDirs.has(dir)}
              {@const dirStatus = directoryStatus(sessions)}
              <div class="dir-block" class:dir-block-active={dirStatus !== 'idle'}>
                <button
                  class="dir-head"
                  class:dir-head-active={dirStatus !== 'idle'}
                  type="button"
                  onclick={() => toggleDir(dir)}
                  aria-expanded={!isCollapsed}
                >
                  <span class="dir-chevron" class:collapsed={isCollapsed} aria-hidden="true"></span>
                  <span class="dir-path mono">{shortPath(dir)}</span>
                  {#if dirStatus !== 'idle'}
                    <span class={`pill pill-compact ${statusClass(dirStatus)}`}>
                      {#if statusShowsDot(dirStatus)}
                        <span class="pill-dot"></span>
                      {/if}
                      {dirStatus}
                    </span>
                  {/if}
                  <span class="dir-count">{sessions.length}</span>
                </button>
                {#if !isCollapsed}
                  <table>
                    <thead>
                      <tr>
                        <th>Title</th>
                        <th>Status</th>
                        <th>Updated</th>
                        <th></th>
                      </tr>
                    </thead>
                    <tbody>
                      {#each sessions as session}
                        <tr
                          class:highlight={drawerSessionId === session.id}
                          onclick={() => openSessionDrawer(session.id)}
                        >
                          <td><span class="task-title">{session.title}</span></td>
                          <td>
                            <span class={`pill ${statusClass(sessionStatus(session))}`}>
                              {#if statusShowsDot(sessionStatus(session))}
                                <span class="pill-dot"></span>
                              {/if}
                              {sessionStatus(session)}
                            </span>
                          </td>
                          <td class="mono">{updatedAgeSeconds(session) ?? '—'}</td>
                          <td class="row-action">
                            <button class="btn btn-ghost btn-sm row-btn" type="button" title="Archive" onclick={(e) => { e.stopPropagation(); archiveSessionInline(session.id); }}>Archive</button>
                            <button class="btn btn-ghost btn-sm row-btn" type="button" title="Dispatch to this session" onclick={(e) => { e.stopPropagation(); prepareDispatchToSession(session.id); }}>Dispatch</button>
                          </td>
                        </tr>
                      {/each}
                    </tbody>
                  </table>
                {/if}
              </div>
            {/each}
          </div>
        {:else if state.sessions.length === 0 && state.tasks.length > 0}
          <div class="group">
            <div class="empty">No top-level sessions.</div>
          </div>
        {/if}
      {/if}
    </section>
  </main>

  <aside class="dispatch">
    <div class="dispatch-head">
      <h2>Actions</h2>
      <button class="btn btn-ghost btn-sm" type="button" onclick={copyApi}>Copy API</button>
    </div>

    <div class="dispatch-tabs">
      <button type="button" class:active={panelTab === 'task'} onclick={() => (panelTab = 'task')}>Task</button>
      <button type="button" class:active={panelTab === 'harness'} onclick={() => (panelTab = 'harness')}>Harness</button>
    </div>

    <div class="dispatch-body">
      {#if panelTab === 'task'}
        <section class="block">
          <div class="block-label">Target</div>
          <div class="segmented">
            <button type="button" class:active={targetMode === 'workspace'} onclick={() => (targetMode = 'workspace')}>New workspace</button>
            <button type="button" class:active={targetMode === 'session'} onclick={() => (targetMode = 'session')}>Existing session</button>
          </div>
          {#if targetMode === 'workspace'}
            <div class="field">
              <label for="cwd">Workspace</label>
              <div class="path-row">
                <input id="cwd" type="text" bind:value={cwd} />
                <button class="btn btn-ghost btn-sm path-pick" type="button" title="Choose folder" aria-label="Choose folder" onclick={() => pickWorkspace('dispatch')}>…</button>
              </div>
            </div>
          {:else}
            <div class="field">
              <label for="project-dir">Project</label>
              <select
                id="project-dir"
                value={selectedProjectDir}
                onchange={(e) => selectProjectDir(e.currentTarget.value)}
              >
                {#each sortedDirectories as dir}
                  {@const sessions = state.sessionsByDirectory[dir]}
                  {#if sessions?.length}
                    <option value={dir}>{projectDirLabel(dir)}</option>
                  {/if}
                {/each}
              </select>
            </div>
            <div class="field">
              <label for="session">Session</label>
              <select id="session" bind:value={selectedSessionId} disabled={sessionsInSelectedProject.length === 0}>
                {#each sessionsInSelectedProject as session}
                  <option value={session.id}>{sessionOptionLabel(session)}</option>
                {/each}
              </select>
            </div>
          {/if}
        </section>

        <section class="block">
          <div class="block-label">Run</div>
          <div class="segmented">
            <button type="button" class:active={mode === 'normal'} onclick={() => (mode = 'normal')}>Normal</button>
            <button type="button" class:active={mode === 'plan'} onclick={() => (mode = 'plan')}>Plan</button>
            <button type="button" class:active={mode === 'review'} onclick={() => (mode = 'review')}>Review</button>
          </div>
        </section>

        <section class="block">
          <div class="block-label">Prompt</div>
          <textarea id="prompt" placeholder="Ask OpenCode to…" bind:value={prompt}></textarea>
          <button class="btn btn-primary" type="button" onclick={dispatchMessage}>
            {targetMode === 'workspace' ? 'Dispatch to Workspace' : 'Send to Session'}
          </button>
          {#if dispatchError}
            <div class="error" style="margin-top: 8px;">{dispatchError}</div>
          {/if}
        </section>
      {:else}
        <section class="block harness-block">
          <div class="segmented">
            <button type="button" class:active={harnessEditorMode === 'guided'} onclick={() => switchHarnessEditor('guided')}>Guided</button>
            <button type="button" class:active={harnessEditorMode === 'raw'} onclick={() => switchHarnessEditor('raw')}>Raw spec</button>
          </div>

          <div class="template-row">
            <span class="dim template-label">Templates</span>
            <div class="template-chips">
              <button class="template-chip" type="button" onclick={() => applyHarnessTemplate('reliability')}>Reliability</button>
              <button class="template-chip" type="button" onclick={() => applyHarnessTemplate('review')}>Review</button>
              <button class="template-chip" type="button" onclick={() => applyHarnessTemplate('refactor')}>Refactor</button>
            </div>
          </div>

          {#if harnessEditorMode === 'guided'}
            <div class="field">
              <label for="harness-name">Task name</label>
              <input id="harness-name" type="text" bind:value={harnessForm.name} placeholder="e.g. Fix SSE reconnect" />
            </div>

            <div class="field">
              <label for="harness-workspace">Workspace</label>
              <div class="path-row">
                <input id="harness-workspace" class="mono" type="text" bind:value={harnessForm.workspace} placeholder="Recent, Browse, or type a path" />
                <button class="btn btn-ghost btn-sm path-pick" type="button" title="Choose folder" aria-label="Choose folder" onclick={() => pickWorkspace('harness')}>…</button>
              </div>
            </div>

            <div class="field">
              <label>Run mode</label>
              <div class="segmented">
                <button type="button" class:active={harnessForm.mode === 'normal'} onclick={() => (harnessForm = { ...harnessForm, mode: 'normal' })}>Normal</button>
                <button type="button" class:active={harnessForm.mode === 'plan'} onclick={() => (harnessForm = { ...harnessForm, mode: 'plan' })}>Plan</button>
                <button type="button" class:active={harnessForm.mode === 'review'} onclick={() => (harnessForm = { ...harnessForm, mode: 'review' })}>Review</button>
              </div>
            </div>

            <div class="field">
              <label for="harness-interval">Check every</label>
              <div class="interval-picker">
                <div class="interval-presets">
                  {#each intervalPresets as minutes}
                    <button
                      type="button"
                      class="interval-preset"
                      class:active={Number(harnessForm.checkInterval) === minutes}
                      onclick={() => (harnessForm = { ...harnessForm, checkInterval: minutes })}
                    >{minutes}m</button>
                  {/each}
                </div>
                <div class="interval-custom">
                  <input
                    id="harness-interval"
                    type="number"
                    min="1"
                    max="120"
                    bind:value={harnessForm.checkInterval}
                  />
                  <span class="interval-unit">min</span>
                </div>
              </div>
            </div>

            <div class="field">
              <label for="harness-goal">Goal</label>
              <textarea id="harness-goal" class="harness-goal" bind:value={harnessForm.goal} placeholder="What should the agent accomplish?"></textarea>
            </div>

            <div class="field">
              <div class="field-head">
                <label>Steps</label>
                <button class="btn btn-ghost btn-sm" type="button" onclick={addHarnessStep}>+ Add</button>
              </div>
              <div class="harness-list">
                {#each harnessForm.steps as _step, i}
                  <div class="harness-list-row">
                    <span class="harness-list-index">{i + 1}</span>
                    <input type="text" bind:value={harnessForm.steps[i]} placeholder="Describe this step" />
                    <button class="list-remove" type="button" aria-label="Remove step" onclick={() => removeHarnessStep(i)}>×</button>
                  </div>
                {/each}
              </div>
            </div>

            <div class="field">
              <div class="field-head">
                <label>Acceptance</label>
                <button class="btn btn-ghost btn-sm" type="button" onclick={addHarnessAcceptance}>+ Add</button>
              </div>
              <div class="harness-list">
                {#each harnessForm.acceptance as _item, i}
                  <div class="harness-list-row">
                    <span class="harness-list-index">✓</span>
                    <input type="text" bind:value={harnessForm.acceptance[i]} placeholder="Done when…" />
                    <button class="list-remove" type="button" aria-label="Remove criterion" onclick={() => removeHarnessAcceptance(i)}>×</button>
                  </div>
                {/each}
              </div>
            </div>

            <div class="harness-preview">
              <span>{harnessPreview.steps} steps</span>
              <span>·</span>
              <span>{harnessPreview.acceptance} criteria</span>
              <span>·</span>
              <span>every {harnessPreview.interval} min</span>
            </div>
          {:else}
            <div class="field">
              <label>Spec format</label>
              <div class="segmented">
                <button type="button" class:active={specFormat === 'yaml'} onclick={() => (specFormat = 'yaml')}>YAML</button>
                <button type="button" class:active={specFormat === 'markdown'} onclick={() => (specFormat = 'markdown')}>Markdown</button>
              </div>
            </div>
            <div class="field">
              <div class="field-head">
                <label for="task-spec">Task spec</label>
                <button class="btn btn-ghost btn-sm" type="button" onclick={loadHarnessExample}>Load example</button>
              </div>
              <textarea id="task-spec" class="spec-editor" placeholder="Define goal, steps, acceptance…" bind:value={taskSpec}></textarea>
            </div>
          {/if}

          <button class="btn btn-primary" type="button" onclick={createHarnessTask}>Start Harness Task</button>
          {#if harnessError}
            <div class="error">{harnessError}</div>
          {/if}
        </section>
      {/if}

      <section class="block">
        <div class="block-label">Selected harness task</div>
        {#if selectedTask}
          <div class="task-card">
            <div class="task-card-head">
              <span class={`pill ${statusClass(selectedTask.status)}`}><span class="pill-dot"></span>{selectedTask.status}</span>
              <span class="mono dim">{stepProgressLabel(selectedTask)} steps</span>
            </div>
            <div class="task-card-title">{selectedTask.name}</div>
            {#if selectedTask.workspace}
              <div class="mono dim">{shortPath(selectedTask.workspace)}</div>
            {/if}
            {#if selectedTask.last_summary}
              <div class="dim">{selectedTask.last_summary}</div>
            {/if}
            {#if selectedTask.error}
              <div class="error">{selectedTask.error}</div>
            {/if}
            <div class="row-actions">
              {#if selectedTask.active_session_id}
                <button class="btn btn-ghost btn-sm" type="button" onclick={() => viewHarnessSession(selectedTask)}>View session</button>
              {/if}
              {#if selectedTask.status === 'paused'}
                <button class="btn btn-ghost btn-sm" type="button" onclick={() => taskAction('resume')}>Resume</button>
              {:else if selectedTask.status === 'archived'}
                <button class="btn btn-ghost btn-sm" type="button" onclick={() => taskAction('resume')}>Restore</button>
              {:else if !['completed', 'failed', 'archived'].includes(selectedTask.status)}
                <button class="btn btn-ghost btn-sm" type="button" onclick={() => taskAction('pause')}>Pause</button>
              {/if}
              {#if !['completed', 'archived'].includes(selectedTask.status)}
                <button class="btn btn-ghost btn-sm" type="button" onclick={() => taskAction('complete')}>Complete</button>
              {/if}
              {#if selectedTask.status !== 'archived'}
                <button class="btn btn-ghost btn-sm" type="button" onclick={() => taskAction('archive')}>Archive</button>
              {/if}
            </div>
            {#if selectedTask.check_log?.length}
              <details class="check-log">
                <summary>Check log ({selectedTask.check_log.length})</summary>
                <ol>
                  {#each selectedTask.check_log.slice().reverse().slice(0, 8) as entry}
                    <li>
                      <span class="mono dim">{updatedAgeSeconds({ updated: entry.at }) ?? '—'}</span>
                      <span class={`pill ${statusClass(entry.status)}`}>{entry.status}</span>
                      <span>{entry.summary}</span>
                    </li>
                  {/each}
                </ol>
              </details>
            {/if}
          </div>
        {:else}
          <div class="dim">No harness tasks yet.</div>
        {/if}
      </section>

      <details class="api-block">
        <summary>API Preview</summary>
        <button class="btn btn-ghost btn-sm copy-api" type="button" onclick={copyApi}>Copy</button>
        <pre class="api-preview">{apiPreview}</pre>
      </details>
    </div>
  </aside>
</div>

{#if drawerSession}
  <div class="drawer-mask" onclick={closeDrawer} role="presentation"></div>
  <aside class="drawer" role="dialog" aria-label="Session detail">
    <header class="drawer-head">
      <div class="drawer-head-main">
        <div class="drawer-eyebrow">Session</div>
        <h3>{drawerSession.title}</h3>
        <div class="mono drawer-dir">{shortPath(drawerSession.directory) || '(unknown directory)'}</div>
        <div class="drawer-status-row">
          <span class={`pill ${statusClass(sessionStatus(drawerSession))}`}>
            {#if statusShowsDot(sessionStatus(drawerSession))}
              <span class="pill-dot"></span>
            {/if}
            {sessionStatus(drawerSession)}
          </span>
          {#if drawerSessionArchived}
            <span class="pill pill-fail" style="display: inline-flex; margin-left: 6px;"><span class="pill-dot"></span>archived</span>
          {/if}
          <span class="dim drawer-loaded">
            {#if drawerLoading}
              loading…
            {:else if drawerLoadedAt}
              updated {Math.max(0, Math.floor((now - drawerLoadedAt) / 1000))}s ago
            {/if}
          </span>
        </div>
      </div>
      <div class="drawer-head-actions">
        <button
          class="btn btn-ghost btn-sm drawer-refresh"
          class:drawer-refresh-busy={drawerLoading}
          type="button"
          title="Refresh session data"
          aria-label="Refresh session data"
          onclick={refreshDrawer}
          disabled={drawerLoading}
        >
          <span class="drawer-refresh-icon" aria-hidden="true">↻</span>
        </button>
        <button class="btn btn-ghost btn-sm" type="button" onclick={closeDrawer}>Close</button>
      </div>
    </header>

    <div class="drawer-tabs">
      <button class:active={drawerTab === 'messages'} type="button" onclick={() => (drawerTab = 'messages')}>Messages</button>
      <button class:active={drawerTab === 'diff'} type="button" onclick={() => (drawerTab = 'diff')}>Diff</button>
      <button class:active={drawerTab === 'meta'} type="button" onclick={() => (drawerTab = 'meta')}>Meta</button>
    </div>

    <div class="drawer-body">
      {#if drawerLoading && drawerMessages.length === 0}
        <div class="empty">Loading…</div>
      {:else if drawerError}
        <div class="error">{drawerError}</div>
      {:else if drawerTab === 'messages'}
        {#if drawerMessages.length === 0}
          <div class="empty">No messages yet.</div>
        {:else}
          <ol class="msg-list">
            {#each drawerMessages as message}
              {@const ts = messageTimestamp(message)}
              {@const lines = summarizeMessage(message)}
              <li class={`msg msg-${messageRole(message)}`}>
                <div class="msg-meta">
                  <span class="mono msg-role">{messageRole(message)}</span>
                  {#if ts}<span class="mono msg-ts">{ts}</span>{/if}
                </div>
                <div class="msg-body">
                  {#each lines as line}
                    <div class="msg-line">{line}</div>
                  {/each}
                </div>
              </li>
            {/each}
          </ol>
        {/if}
      {:else if drawerTab === 'diff'}
        {#if drawerDiff.length === 0}
          <div class="empty">No diff recorded.</div>
        {:else}
          <ul class="diff-list">
            {#each drawerDiff as file}
              {@const filePath = file.file || file.path || file.filename || '(unnamed file)'}
              {@const additions = file.additions ?? file.added ?? file.linesAdded}
              {@const deletions = file.deletions ?? file.removed ?? file.linesRemoved}
              <li>
                <div class="diff-file-head">
                  <span class="mono diff-file-path">{filePath}</span>
                  <span class="diff-file-stats">
                    {#if additions !== undefined && additions !== null}<span class="diff-add">+{additions}</span>{/if}
                    {#if deletions !== undefined && deletions !== null}<span class="diff-del">-{deletions}</span>{/if}
                  </span>
                </div>
                {#if file.patch || file.diff || file.hunks}
                  <pre class="diff-block">{file.patch || file.diff}</pre>
                {:else}
                  <pre class="diff-block diff-block-meta">{JSON.stringify(file, null, 2)}</pre>
                {/if}
              </li>
            {/each}
          </ul>
        {/if}
      {:else}
        <div class="meta-grid">
          {#each Object.entries(metaFields(drawerSession)) as [label, value]}
            <div class="meta-row">
              <div class="meta-label">{label}</div>
              <div class="meta-value mono">{value}</div>
            </div>
          {/each}
        </div>
        <details class="meta-raw">
          <summary>Raw JSON</summary>
          <pre class="payload-block">{JSON.stringify(drawerSession, null, 2)}</pre>
        </details>
      {/if}
    </div>

    <footer class="drawer-foot">
      {#if drawerSessionArchived}
        <button class="btn btn-ghost" type="button" onclick={unarchiveSessionFromPopover(drawerSessionId)}>Unarchive</button>
      {:else}
        <button class="btn btn-ghost" type="button" onclick={archiveSessionFromDrawer}>Archive</button>
      {/if}
    </footer>
  </aside>
{/if}

{#if folderPickerOpen}
  <div class="modal-mask" onclick={closeFolderPicker} role="presentation"></div>
  <div class="modal folder-picker-modal" role="dialog" aria-modal="true" aria-label="Select workspace folder">
    <h3 class="modal-title">Select workspace</h3>
    <div class="folder-picker-path mono" title={folderBrowsePath}>{folderBrowsePath || '—'}</div>
    {#if folderPickerError}
      <div class="error">{folderPickerError}</div>
    {/if}
    {#if folderPickerLoading}
      <div class="dim folder-picker-status">Loading…</div>
    {:else}
      <ul class="folder-picker-list">
        {#if folderBrowseParent}
          <li>
            <button class="folder-picker-item" type="button" onclick={() => loadBrowse(folderBrowseParent)}>
              <span class="folder-picker-icon">↩</span>
              <span>Parent folder</span>
            </button>
          </li>
        {/if}
        {#each folderBrowseChildren as child}
          <li>
            <button class="folder-picker-item" type="button" onclick={() => loadBrowse(child.path)}>
              <span class="folder-picker-icon">▸</span>
              <span>{child.name}</span>
            </button>
          </li>
        {/each}
        {#if !folderBrowseParent && folderBrowseChildren.length === 0}
          <li class="dim folder-picker-empty">No subfolders here.</li>
        {/if}
      </ul>
    {/if}
    <div class="folder-picker-foot">
      <button class="btn btn-primary folder-picker-select" type="button" onclick={confirmFolderPick} disabled={!folderBrowsePath}>
        Select this folder
      </button>
      <button class="btn btn-ghost folder-picker-cancel" type="button" onclick={closeFolderPicker}>Cancel</button>
    </div>
  </div>
{/if}

{#if confirmDialog}
  <div class="modal-mask" onclick={() => resolveConfirm(false)} role="presentation"></div>
  <div class="modal" role="dialog" aria-modal="true" aria-label={confirmDialog.title}>
    <h3 class="modal-title">{confirmDialog.title}</h3>
    <p class="modal-message">{confirmDialog.message}</p>
    <div class="modal-actions">
      <button class="btn btn-ghost" type="button" onclick={() => resolveConfirm(false)}>Cancel</button>
      <button
        class={`btn ${confirmDialog.danger ? 'btn-danger-solid' : 'btn-primary'}`}
        type="button"
        autofocus
        onclick={() => resolveConfirm(true)}
      >{confirmDialog.confirmLabel}</button>
    </div>
  </div>
{/if}
