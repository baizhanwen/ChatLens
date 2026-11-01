/* Local setup and isolated focus scopes. Only names and public state enter this page. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const stages = new Set(['needs_initialization', 'ready_to_select', 'starting', 'running', 'error']);
  const state = { setup: null, generation: null, connected: false, error: null, busy: null, epoch: 0, setupRequest: 0, timer: null, failures: 0, listRequest: 0, listLoading: false, listError: false, listMore: false, rows: [], selected: new Map(), selectionSeeded: false, pickerGeneration: null, loadedGeneration: null, editing: false, redirecting: false, recovering: false, enterWorkspace: false, stayOnSetup: new URLSearchParams(location.search).get('manage') === '1' };
  const renderedLists = new Map();
  let profilesSignature = '';
  let searchTimer;

  function node(tag, className, text) { const result = document.createElement(tag); if (className) result.className = className; if (text !== undefined) result.textContent = text; return result; }
  function error(code, status = 0) { return Object.assign(new Error(), { code, status }); }
  function ignored(problem) { return ['stale_response', 'scope_changed'].includes(problem?.code); }
  function allowedOrigin() { return location.protocol === 'http:' && location.hostname === '127.0.0.1'; }
  function validGeneration(value) { return typeof value === 'string' && /^[a-zA-Z0-9_-]{16,128}$/.test(value); }
  function working() { return state.busy !== null || state.setup?.initialization?.state === 'working' || state.setup?.stage === 'starting' || state.setup?.scope_switching === true; }
  function catalogReady() { return state.setup?.catalog?.state === 'ready' || state.setup?.stage === 'ready_to_select'; }
  function canSelect() { return state.connected && Boolean(state.generation) && state.editing && ['ready_to_select', 'running', 'error'].includes(state.setup?.stage) && catalogReady() && !working(); }
  function scopeLimit() { const value = state.setup?.scope_limits?.total; return Number.isSafeInteger(value) && value > 0 ? Math.min(value, 50) : 50; }
  function resetPicker() { state.listRequest++; state.rows = []; state.selected.clear(); state.selectionSeeded = false; state.pickerGeneration = null; state.loadedGeneration = null; state.listLoading = false; state.listError = false; state.listMore = false; state.editing = false; clearTimeout(searchTimer); $('setup-conversation-search').value = ''; renderedLists.clear(); }

  async function api(route, { method = 'GET', body, token } = {}) {
    if (!allowedOrigin()) throw error('loopback_required');
    const generation = state.generation;
    const epoch = state.epoch;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 12000);
    try {
      const headers = token ? { Authorization: 'Bearer ' + token } : {};
      if (generation) headers['X-ChatLens-Scope'] = generation;
      if (method === 'POST') headers['Content-Type'] = 'application/json';
      const response = await fetch(new URL(route, location.origin), { method, headers, credentials: 'same-origin', cache: 'no-store', signal: controller.signal, body: method === 'POST' ? JSON.stringify(body || {}) : undefined });
      let result;
      try { result = await response.json(); } catch { throw error('invalid_response', response.status); }
      if (epoch !== state.epoch || generation !== state.generation) throw error('stale_response');
      if (!response.ok) {
        const code = /^[a-z0-9_]{1,100}$/.test(result?.error?.code || '') ? result.error.code : 'request_failed';
        if (code === 'scope_changed') recoverScope();
        throw error(code, response.status);
      }
      if (typeof result?.scope_generation === 'string' && result.scope_generation !== generation && !['/api/session', '/api/setup', '/api/setup/select', '/api/setup/profile'].includes(route)) { recoverScope(); throw error('scope_changed', 409); }
      return result;
    } finally { clearTimeout(timeout); }
  }

  function setError(problem) { if (ignored(problem)) return; state.connected = false; state.error = { code: problem?.code || 'connection_failed', status: problem?.status || 0 }; state.failures++; render(); }
  function recoverScope() {
    if (state.recovering) return;
    state.recovering = true;
    state.stayOnSetup = true;
    state.enterWorkspace = false;
    state.epoch++;
    state.setupRequest++;
    clearTimeout(state.timer);
    resetPicker();
    state.setup = null;
    state.generation = null;
    state.connected = false;
    state.busy = null;
    state.error = { code: 'scope_changed', status: 409 };
    render();
    Promise.resolve().then(connect).finally(() => { state.recovering = false; });
  }

  function acceptSetup(value) {
    if (!value || !stages.has(value.stage) || !value.account || typeof value.account.detected !== 'boolean' || value.account.display_name != null && typeof value.account.display_name !== 'string') throw error('invalid_response');
    if (value.scope_generation !== undefined && !validGeneration(value.scope_generation)) throw error('invalid_response');
    const nextGeneration = value.scope_generation || state.generation;
    const changed = nextGeneration !== state.generation;
    if (changed) { resetPicker(); if (!state.enterWorkspace) state.stayOnSetup = true; }
    state.generation = nextGeneration;
    state.setup = value;
    state.connected = true;
    state.error = null;
    state.failures = 0;
    if (value.stage === 'ready_to_select' && !value.scope_switching) state.editing = true;
    if (value.stage === 'starting' || value.scope_switching) { state.editing = false; state.listRequest++; state.listLoading = false; }
    render();
    if (canSelect() && state.loadedGeneration !== state.generation && !state.listLoading) loadConversations();
    if (value.stage === 'running' && (state.enterWorkspace || !state.stayOnSetup) && !state.redirecting) openWorkspace();
  }
  function openWorkspace() { if (!state.connected || state.setup?.stage !== 'running' || state.setup?.scope_switching || state.redirecting) return; state.redirecting = true; const target = ['/', '/index.html'].includes(state.setup.workspace_path) ? state.setup.workspace_path : '/'; location.assign(target); }
  function schedule(epoch) { clearTimeout(state.timer); if (state.error?.status === 401 || !allowedOrigin() || epoch !== state.epoch || state.redirecting) return; state.timer = setTimeout(() => poll(epoch), state.failures ? Math.min(10000, 1000 * 2 ** Math.min(state.failures, 4)) : 1000); }

  async function connect() {
    const epoch = ++state.epoch, request = ++state.setupRequest;
    clearTimeout(state.timer); state.error = null; state.connected = false; render();
    try {
      let token = new URLSearchParams(location.hash.slice(1)).get('token');
      const session = await api('/api/session', { token }); token = null;
      if (epoch !== state.epoch) return;
      const generation = validGeneration(session?.scope_generation) ? session.scope_generation : null;
      if (session?.scope_generation !== undefined && !generation) throw error('invalid_response');
      if (generation !== state.generation) { resetPicker(); state.setup = null; }
      state.generation = generation;
      if (location.hash) history.replaceState(null, '', location.pathname + location.search);
      const value = await api('/api/setup');
      if (epoch === state.epoch && request === state.setupRequest) acceptSetup(value);
    } catch (problem) { if (epoch === state.epoch) setError(problem); }
    finally { schedule(epoch); }
  }
  async function poll(epoch = state.epoch) {
    if (state.busy) { schedule(epoch); return; }
    const request = ++state.setupRequest;
    try { const value = await api('/api/setup'); if (epoch === state.epoch && request === state.setupRequest) acceptSetup(value); }
    catch (problem) { if (epoch === state.epoch && request === state.setupRequest) setError(problem); }
    finally { schedule(epoch); }
  }

  const actionCopy = {
    save_drafts: ['请先保存微信草稿', '在微信中保存尚未发送的草稿。完成后回到此页，继续本机初始化。', '我已保存草稿，继续'],
    exit_wechat: ['请正常退出当前微信', '请先保存微信草稿，再从微信菜单正常退出。完成后回到此页；程序会继续检查，也可以点击下方按钮。', '我已退出微信，继续'],
    confirm_login: ['请在微信窗口完成登录', '请本人在打开的微信窗口点击「进入微信」。如果显示二维码或手机确认，请在本机完成，再回到此页。', '我已完成登录，继续'],
    grant_access: ['请完成本机文件访问授权', '请按本机启动器的说明，在系统设置中开启所需文件访问权限，再回到此页继续。', '我已开启权限，继续']
  };
  const phaseCopy = { detecting: '正在识别当前登录的微信', detecting_account: '正在识别当前登录的微信', validating_reader: '正在验证本机读取能力', initializing: '正在初始化本机读取能力', preparing_copy: '正在准备本机连接', waiting_for_exit: '等待你正常退出微信', waiting_for_login: '等待你在本机完成登录', validating_keys: '正在验证本机读取能力', ready: '本机读取能力已就绪', baseline: '正在准备所选关注范围', waiting_background: '等待微信保持后台', loading_conversations: '正在读取会话名称' };
  const count = value => Number.isSafeInteger(value) && value >= 0 ? value : null;
  function progress() {
    const value = state.setup?.progress || {}, complete = count(value.completed), total = count(value.total), seconds = count(value.elapsed_seconds);
    const detail = complete === null ? '当前阶段暂无可计算进度，请等待本机状态更新。' : total !== null && total >= complete ? '已完成 ' + complete + ' / ' + total : '已完成 ' + complete;
    const phase = typeof value.phase === 'string' && value.phase ? phaseCopy[value.phase] || (/[\u4e00-\u9fff]/.test(value.phase) ? value.phase : '正在处理本机连接') : state.setup?.stage === 'starting' ? '等待本机采集就绪' : '等待本机状态更新';
    for (const prefix of ['progress', 'starting']) { $(prefix + '-phase').textContent = phase; $(prefix + '-count').textContent = detail; $(prefix + '-elapsed').textContent = seconds === null ? '' : String(Math.floor(seconds / 60)).padStart(2, '0') + ':' + String(seconds % 60).padStart(2, '0'); }
  }

  function render() {
    const setup = state.setup, stage = setup?.stage, initialization = setup?.initialization || {}, active = state.connected, action = initialization.state === 'needs_user_action' ? actionCopy[initialization.action] : null, isWorking = working();
    $('connection-notice').hidden = !state.error;
    const expired = state.error?.status === 401, fileMode = !allowedOrigin(), scopeChanged = state.error?.code === 'scope_changed';
    $('connection-title').textContent = fileMode ? '请从本机启动器打开连接页面' : expired ? '本机连接会话已失效' : scopeChanged ? '关注范围已变化' : '暂时无法连接本机服务';
    $('connection-description').textContent = fileMode ? '这个页面需要本机连接服务。请运行 ChatLens 启动器，使用它自动打开的页面。' : expired ? '请回到本机 ChatLens 启动器，重新打开它提供的连接页面。' : scopeChanged ? '旧选择已清除，正在重新读取当前关注范围。不会重试之前的切换操作。' : '页面保留当前范围的选择。请确认本机启动器仍在运行，然后重新连接。';
    $('retry-connection').disabled = expired || fileMode;
    $('retry-connection').textContent = expired || fileMode ? '请从启动器重新打开' : '重新连接';
    $('account-title').textContent = setup?.account?.detected ? setup.account.display_name?.trim() || '当前登录微信已识别' : setup ? '等待本机微信登录' : '正在识别当前登录的微信';
    $('account-description').textContent = setup?.account?.detected ? '仅在当前登录的微信中选择需要关注的沟通。' : '请在这台电脑打开微信并完成登录，页面会自动检查。';
    const stageCopy = { needs_initialization: isWorking ? '正在初始化' : action ? '等待本人操作' : '需要首次连接', ready_to_select: '可以选择会话', starting: '正在切换范围', running: '工作台已就绪', error: '需要处理' };
    $('setup-stage').textContent = state.error ? '连接中断' : !active ? '正在连接' : stageCopy[stage] || '正在检查';
    $('setup-stage').className = 'tag tag-' + (state.error || stage === 'error' ? 'failure' : stage === 'ready_to_select' || stage === 'running' ? 'success' : isWorking ? 'running' : 'waiting');
    for (const [key, field] of [['wechat', 'wechat_running'], ['access', 'access_ready'], ['reader', 'reader_ready']]) { const value = setup?.checks?.[field]; $(key + '-check').textContent = value === true ? '已就绪' : value === false ? '需要处理' : '检查中'; $(key + '-check-dot').className = 'check-dot' + (value === true ? ' is-ready' : value === false ? ' is-missing' : ''); }
    const step = !setup?.account?.detected ? 'account' : ['ready_to_select', 'starting', 'running'].includes(stage) ? 'select' : 'initialize', order = ['account', 'initialize', 'select'];
    document.querySelectorAll('[data-step]').forEach(item => { if (item.dataset.step === step) item.setAttribute('aria-current', 'step'); else item.removeAttribute('aria-current'); item.classList.toggle('is-complete', order.indexOf(item.dataset.step) < order.indexOf(step)); });
    $('initialization-card').hidden = ['ready_to_select', 'starting', 'running', 'error'].includes(stage);
    $('initialization-progress').hidden = !isWorking || stage === 'starting';
    $('user-action-note').hidden = !action;
    if (action) { $('user-action-title').textContent = action[0]; $('user-action-description').textContent = typeof initialization.description === 'string' && initialization.description.trim() ? initialization.description : action[1]; }
    $('initialize-button').disabled = !active || !state.generation || Boolean(state.busy) || initialization.state === 'working' || (!setup?.account?.detected && !action);
    $('initialize-button').classList.toggle('is-loading', initialization.state === 'working' || state.busy === 'initialize');
    $('initialize-button').setAttribute('aria-busy', String(initialization.state === 'working' || state.busy === 'initialize'));
    $('initialize-button').textContent = !active ? '等待本机连接' : isWorking ? '正在初始化' : action ? action[2] : setup?.account?.detected ? '开始本机初始化' : '等待微信登录';
    $('initialize-help').textContent = action ? '请本人完成上方操作，页面也会自动继续检查。' : isWorking ? '正在进行本机初始化，请按页面出现的提示操作。' : '不会自动退出你的微信；需要本人操作时会明确提示。';
    $('setup-error').hidden = stage !== 'error';
    $('setup-error-description').textContent = typeof initialization.description === 'string' && initialization.description.trim() ? initialization.description : '本机连接没有完成。请确认微信状态和文件访问权限，再尝试初始化。';
    $('setup-reason').textContent = /^[a-z0-9_]{1,100}$/.test(setup?.reason || '') ? setup.reason : '暂未提供其他状态信息。';
    $('retry-initialization').disabled = !active || !state.generation || isWorking || !setup?.account?.detected;
    $('check-status').disabled = Boolean(state.busy);
    $('scope-capability-note').hidden = !active || Boolean(state.generation);
    $('scope-manager').hidden = !state.generation || !['running', 'error'].includes(stage) || setup?.scope_switching === true;
    $('active-scope-label').textContent = typeof setup?.active_scope?.label === 'string' ? setup.active_scope.label : '当前关注范围';
    const scope = setup?.active_scope;
    $('active-scope-count').textContent = scope && count(scope.count) !== null ? scope.count + ' 个会话 · ' + (count(scope.private_count) || 0) + ' 个私聊 · ' + (count(scope.group_count) || 0) + ' 个群聊' : '选择后会建立独立的本机记录';
    $('manage-conversations').disabled = !active || isWorking || setup?.checks?.reader_ready !== true;
    $('manage-conversations').textContent = setup?.catalog?.state === 'loading' ? '正在读取会话名称' : state.editing ? '查看会话选择' : '修改关注范围';
    $('manage-conversations').classList.toggle('is-loading', setup?.catalog?.state === 'loading');
    $('open-workspace').disabled = !active || stage !== 'running' || isWorking;
    $('selection-card').hidden = !state.editing || !state.generation || stage === 'starting';
    $('selection-limit').textContent = '可选 1–' + scopeLimit() + ' 个会话';
    $('starting-card').hidden = stage !== 'starting';
    $('starting-title').textContent = '正在启用关注范围';
    $('starting-description').textContent = typeof initialization.description === 'string' && initialization.description.trim() ? initialization.description : '正在准备所选范围。请切回此页面，让微信保持后台，等待状态更新。';
    $('starting-button').textContent = '正在启用关注范围';
    $('setup-conversation-search').disabled = !canSelect() || !state.selectionSeeded;
    progress(); renderSelection(); renderProfiles();
  }

  function pickerEmpty(title, description) { const result = node('div', 'picker-empty'); result.append(node('strong', '', title), node('p', '', description)); return result; }
  function renderSelection() {
    const loading = state.listLoading || state.setup?.catalog?.state === 'loading';
    $('setup-conversation-search').disabled = !canSelect() || !state.selectionSeeded;
    for (const type of ['private', 'group']) {
      const list = $(type + '-list'), rows = state.rows.filter(row => row.type === type);
      list.setAttribute('aria-busy', String(loading));
      $(type + '-result-count').textContent = loading ? '读取中' : String(rows.length);
      const signature = JSON.stringify([rows, [...state.selected.keys()], canSelect(), loading, state.listError, scopeLimit()]);
      if (renderedLists.get(type) !== signature) {
        const focused = list.contains(document.activeElement) ? document.activeElement.dataset.pickerId : null;
        list.replaceChildren();
        for (const row of rows) {
          const selected = state.selected.has(row.id), disabled = !canSelect() || loading || state.listError || !selected && state.selected.size >= scopeLimit(), generation = state.generation;
          const label = node('label', 'picker-row' + (selected ? ' is-selected' : '') + (disabled ? ' is-disabled' : ''));
          const checkbox = node('input', 'picker-checkbox'); checkbox.type = 'checkbox'; checkbox.checked = selected; checkbox.disabled = disabled; checkbox.dataset.pickerId = row.id; checkbox.setAttribute('aria-label', '关注' + row.name);
          label.append(node('span', 'avatar ' + (type === 'group' ? 'sand' : 'sage'), row.name.slice(0, type === 'group' ? 2 : 1)), node('strong', '', row.name), checkbox);
          checkbox.addEventListener('change', () => { if (!canSelect() || generation !== state.generation) return; if (checkbox.checked && state.selected.size >= scopeLimit()) { checkbox.checked = false; $('setup-announcer').textContent = '最多选择 ' + scopeLimit() + ' 个会话。'; return; } if (checkbox.checked) state.selected.set(row.id, row); else state.selected.delete(row.id); renderSelection(); $('setup-announcer').textContent = '已选择 ' + state.selected.size + ' 个会话。'; });
          list.append(label);
        }
        if (!rows.length) list.append(pickerEmpty(loading ? '正在读取会话名称' : state.listError ? '暂时无法读取会话' : '没有匹配的' + (type === 'group' ? '群聊' : '私聊'), loading ? '只读取当前账号的会话名称，当前采集保持运行。' : state.listError ? '请重新读取，当前范围内的选择会保留。' : '试试其他名称，或清空搜索。'));
        renderedLists.set(type, signature);
        if (focused) [...list.querySelectorAll('.picker-checkbox')].find(checkbox => checkbox.dataset.pickerId === focused)?.focus({ preventScroll: true });
      }
      const selected = [...state.selected.values()].filter(row => row.type === type);
      $('selected-' + type).textContent = selected.length ? selected.length + ' 个' : '尚未选择';
    }
    $('selected-conversations').replaceChildren();
    for (const row of state.selected.values()) { const generation = state.generation, chip = node('button', 'selected-chip', row.name + ' ×'); chip.type = 'button'; chip.disabled = !canSelect(); chip.setAttribute('aria-label', '移除' + row.name); chip.addEventListener('click', () => { if (generation !== state.generation || !canSelect()) return; state.selected.delete(row.id); renderSelection(); }); $('selected-conversations').append(chip); }
    const total = state.selected.size;
    $('selection-count').textContent = '已选择 ' + total + ' / ' + scopeLimit();
    $('start-selected').disabled = total < 1 || total > scopeLimit() || !state.selectionSeeded || !canSelect() || loading || state.listError || state.pickerGeneration !== state.generation;
    $('start-selected').classList.toggle('is-loading', state.busy === 'select'); $('start-selected').setAttribute('aria-busy', String(state.busy === 'select'));
    $('start-selected').textContent = state.busy === 'select' ? '正在确认所选范围' : '启用所选关注范围';
    $('conversation-feedback').hidden = !loading && !state.listError && !state.listMore;
    $('conversation-feedback-text').textContent = loading ? '正在查找会话名称，已选内容会保留…' : state.listError ? '读取没有完成，请重新尝试。' : '只展示部分匹配结果，请输入更具体的名称继续查找。';
    $('retry-conversations').hidden = !state.listError; $('retry-conversations').disabled = !canSelect();
  }
  function renderProfiles() {
    const rows = Array.isArray(state.setup?.profiles) ? state.setup.profiles.filter(row => typeof row?.id === 'string' && typeof row.label === 'string' && count(row.count) !== null) : [];
    $('profiles-card').hidden = !state.generation || !rows.length;
    const signature = JSON.stringify([state.generation, rows, state.connected, working()]); if (signature === profilesSignature) return; profilesSignature = signature;
    const list = $('saved-profiles'); list.replaceChildren();
    for (const profile of rows) { const generation = state.generation, row = node('article', 'scope-profile' + (profile.active ? ' is-active' : '')); const copy = node('div'); copy.append(node('strong', '', profile.label), node('p', '', profile.count + ' 个会话 · ' + (count(profile.private_count) || 0) + ' 个私聊 · ' + (count(profile.group_count) || 0) + ' 个群聊')); const button = node('button', 'button', profile.active ? '当前使用' : '切回此范围'); button.type = 'button'; button.dataset.profileId = profile.id; button.disabled = profile.active || !state.connected || working(); button.addEventListener('click', () => { if (generation === state.generation) mutate('profile', profile.id); }); row.append(copy, button); list.append(row); }
  }

  async function loadConversations() {
    if (!canSelect()) return;
    const request = ++state.listRequest, generation = state.generation, query = state.selectionSeeded ? $('setup-conversation-search').value.trim() : ''; state.listLoading = true; state.listError = false; renderSelection();
    try {
      const result = await api('/api/setup/conversations?' + new URLSearchParams({ q: query }));
      if (request !== state.listRequest || generation !== state.generation || !state.editing) return;
      if (!Array.isArray(result?.conversations)) throw error('invalid_response');
      const seen = new Set();
      const rows = result.conversations.map(row => { if (!row || typeof row.id !== 'string' || !row.id || seen.has(row.id) || typeof row.name !== 'string' || !row.name.trim() || !['private', 'group'].includes(row.type)) throw error('invalid_response'); seen.add(row.id); return { id: row.id, name: row.name, type: row.type, selected: row.selected === true }; });
      if (!state.selectionSeeded) {
        const selected = rows.filter(row => row.selected);
        if (selected.length > scopeLimit()) throw error('invalid_response');
        state.selected = new Map(selected.map(row => [row.id, row]));
        state.selectionSeeded = true;
      }
      state.rows = rows;
      state.listMore = result.has_more === true; state.pickerGeneration = generation; state.loadedGeneration = generation;
    } catch (problem) { if (request === state.listRequest && !ignored(problem)) { state.listError = true; if (problem.status === 401) setError(problem); } }
    finally { if (request === state.listRequest) { state.listLoading = false; renderSelection(); } }
  }
  async function mutate(kind, profileId) {
    if (!state.connected || !state.generation || working()) return;
    if (kind === 'select' && (!canSelect() || !state.selectionSeeded || state.selected.size < 1 || state.selected.size > scopeLimit() || state.pickerGeneration !== state.generation || state.listLoading || state.listError)) return;
    if (kind === 'profile' && !state.setup?.profiles?.some(row => row.id === profileId && row.active !== true)) return;
    const epoch = state.epoch, generation = state.generation;
    if (kind === 'select' || kind === 'profile') state.enterWorkspace = true;
    if (kind === 'catalog') state.editing = true;
    state.busy = kind; state.setupRequest++; render();
    try {
      const body = kind === 'select' ? { conversation_ids: [...state.selected.keys()], scope_generation: generation } : kind === 'profile' ? { profile_id: profileId, scope_generation: generation } : {};
      const result = await api('/api/setup/' + kind, { method: 'POST', body });
      if (epoch !== state.epoch) return;
      if (stages.has(result?.stage) && typeof result.account?.detected === 'boolean') acceptSetup(result); else acceptSetup(await api('/api/setup'));
    } catch (problem) { if (epoch === state.epoch && !ignored(problem)) { state.enterWorkspace = false; setError(problem); } }
    finally { if (epoch === state.epoch) { state.busy = null; render(); if (canSelect() && state.loadedGeneration !== state.generation && !state.listLoading) loadConversations(); schedule(state.epoch); } }
  }
  function beginSelection() {
    if (!state.connected || !state.generation || working()) return;
    if (!catalogReady()) { mutate('catalog'); return; }
    state.editing = true;
    render();
    if (state.loadedGeneration !== state.generation && !state.listLoading) loadConversations();
    $('selection-card').scrollIntoView({ block: 'start', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
  }

  $('initialize-button').addEventListener('click', () => mutate('initialize')); $('retry-initialization').addEventListener('click', () => mutate('initialize'));
  $('start-selected').addEventListener('click', () => mutate('select')); $('manage-conversations').addEventListener('click', beginSelection); $('open-workspace').addEventListener('click', openWorkspace);
  $('retry-connection').addEventListener('click', connect); $('check-status').addEventListener('click', () => poll()); $('retry-conversations').addEventListener('click', loadConversations);
  for (const type of ['private', 'group']) $(type + '-list').addEventListener('keydown', event => { if (!['ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return; const rows = [...$(type + '-list').querySelectorAll('.picker-checkbox:not(:disabled)')]; if (!rows.length) return; const current = rows.indexOf(document.activeElement), next = event.key === 'Home' ? 0 : event.key === 'End' ? rows.length - 1 : (current + (['ArrowUp', 'ArrowLeft'].includes(event.key) ? -1 : 1) + rows.length) % rows.length; event.preventDefault(); rows[next].focus(); });
  $('setup-conversation-search').addEventListener('input', () => { clearTimeout(searchTimer); state.listRequest++; state.rows = []; state.listLoading = true; state.listError = false; renderSelection(); searchTimer = setTimeout(loadConversations, 250); });
  window.addEventListener('pagehide', () => { state.epoch++; state.setupRequest++; clearTimeout(state.timer); resetPicker(); state.setup = null; state.generation = null; state.connected = false; state.busy = null; state.enterWorkspace = false; render(); });
  window.addEventListener('pageshow', event => { if (event.persisted) { resetPicker(); state.setup = null; state.generation = null; state.stayOnSetup = true; state.redirecting = false; connect(); } });
  if (allowedOrigin()) connect(); else { setError(error('loopback_required')); $('setup-announcer').textContent = '请从本机启动器打开连接页面。'; }
})();
