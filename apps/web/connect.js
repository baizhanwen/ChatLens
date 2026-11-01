/* Local onboarding only: no source paths, keys, chat bodies or cloud services. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const stages = new Set(['needs_initialization', 'ready_to_select', 'starting', 'running', 'error']);
  const state = { setup: null, connected: false, error: null, busy: null, epoch: 0, setupRequest: 0, timer: null, failures: 0, listRequest: 0, listLoading: false, listError: false, listMore: false, rows: [], selected: { private: null, group: null }, redirecting: false };
  let searchTimer;
  const renderedLists = new Map();

  function node(tag, className, text) {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (text !== undefined) result.textContent = text;
    return result;
  }

  function error(code, status = 0) { return Object.assign(new Error(), { code, status }); }
  function allowedOrigin() { return location.protocol === 'http:' && location.hostname === '127.0.0.1'; }
  function working() { return state.busy !== null || state.setup?.initialization?.state === 'working' || state.setup?.stage === 'starting'; }
  function canSelect() { return state.connected && state.setup?.stage === 'ready_to_select' && !working(); }

  async function api(route, { method = 'GET', body, token } = {}) {
    if (!allowedOrigin()) throw error('loopback_required');
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 12000);
    try {
      const headers = token ? { Authorization: 'Bearer ' + token } : {};
      if (method === 'POST') headers['Content-Type'] = 'application/json';
      const response = await fetch(new URL(route, location.origin), { method, headers, credentials: 'same-origin', cache: 'no-store', signal: controller.signal, body: method === 'POST' ? JSON.stringify(body || {}) : undefined });
      let result;
      try { result = await response.json(); } catch { throw error('invalid_response', response.status); }
      if (!response.ok) throw error(/^[a-z0-9_]{1,100}$/.test(result?.error?.code || '') ? result.error.code : 'request_failed', response.status);
      return result;
    } finally { clearTimeout(timeout); }
  }

  function setError(problem) {
    state.connected = false;
    state.error = { code: problem?.code || 'connection_failed', status: problem?.status || 0 };
    state.failures++;
    render();
  }

  function acceptSetup(value) {
    if (!value || !stages.has(value.stage) || !value.account || typeof value.account.detected !== 'boolean') throw error('invalid_response');
    if (value.account.display_name !== null && value.account.display_name !== undefined && typeof value.account.display_name !== 'string') throw error('invalid_response');
    const previousStage = state.setup?.stage;
    state.setup = value;
    state.connected = true;
    state.error = null;
    state.failures = 0;
    if (value.stage !== 'ready_to_select' && previousStage === 'ready_to_select') {
      state.listRequest++;
      state.listLoading = false;
      if (!['starting', 'running'].includes(value.stage)) { state.rows = []; state.selected = { private: null, group: null }; }
    }
    render();
    if (value.stage === 'ready_to_select' && previousStage !== 'ready_to_select') loadConversations();
    if (value.stage === 'running' && !state.redirecting) {
      state.redirecting = true;
      // Only the known same-origin workspace is a valid destination.
      const target = ['/', '/index.html'].includes(value.workspace_path) ? value.workspace_path : '/';
      location.assign(target);
    }
  }

  function schedule(epoch) {
    clearTimeout(state.timer);
    if (state.error?.status === 401 || !allowedOrigin() || epoch !== state.epoch || state.redirecting) return;
    state.timer = setTimeout(() => poll(epoch), state.failures ? Math.min(10000, 1000 * 2 ** Math.min(state.failures, 4)) : 1000);
  }

  async function connect() {
    const epoch = ++state.epoch;
    const request = ++state.setupRequest;
    clearTimeout(state.timer);
    state.error = null;
    state.connected = false;
    render();
    try {
      let token = new URLSearchParams(location.hash.slice(1)).get('token');
      await api('/api/session', { token });
      token = null;
      if (epoch !== state.epoch) return;
      if (location.hash) history.replaceState(null, '', location.pathname + location.search);
      const value = await api('/api/setup');
      if (epoch === state.epoch && request === state.setupRequest) acceptSetup(value);
    } catch (problem) { if (epoch === state.epoch) setError(problem); }
    finally { schedule(epoch); }
  }

  async function poll(epoch = state.epoch) {
    if (state.busy) { schedule(epoch); return; }
    const request = ++state.setupRequest;
    try {
      const value = await api('/api/setup');
      if (epoch === state.epoch && request === state.setupRequest) acceptSetup(value);
    } catch (problem) { if (epoch === state.epoch && request === state.setupRequest) setError(problem); }
    finally { schedule(epoch); }
  }

  const actionCopy = {
    save_drafts: ['请先保存微信草稿', '在微信中保存尚未发送的草稿。完成后回到此页，继续本机初始化。', '我已保存草稿，继续'],
    exit_wechat: ['请正常退出当前微信', '请先保存微信草稿，再从微信菜单正常退出。完成后回到此页；程序会继续检查，也可以点击下方按钮。', '我已退出微信，继续'],
    confirm_login: ['请在微信窗口完成登录', '请本人在打开的微信窗口点击「进入微信」。如果显示二维码或手机确认，请在本机完成，再回到此页。', '我已完成登录，继续'],
    grant_access: ['请完成本机文件访问授权', '请按本机启动器的说明，在系统设置中开启所需文件访问权限，再回到此页继续。', '我已开启权限，继续']
  };
  const phaseCopy = { detecting: '正在识别当前登录的微信', detecting_account: '正在识别当前登录的微信', validating_reader: '正在验证本机读取能力', initializing: '正在初始化本机读取能力', preparing_copy: '正在准备本机连接', waiting_for_exit: '等待你正常退出微信', waiting_for_login: '等待你在本机完成登录', validating_keys: '正在验证本机读取能力', ready: '本机读取能力已就绪', baseline: '正在建立新消息起点', waiting_background: '等待微信保持后台', loading_conversations: '正在读取会话名称' };
  const count = value => Number.isSafeInteger(value) && value >= 0 ? value : null;

  function progress() {
    const value = state.setup?.progress || {};
    const complete = count(value.completed), total = count(value.total), seconds = count(value.elapsed_seconds);
    const detail = complete === null ? '当前阶段暂无可计算进度，请等待本机状态更新。' : total !== null && total >= complete ? '已完成 ' + complete + ' / ' + total : '已完成 ' + complete;
    const phase = typeof value.phase === 'string' && value.phase ? phaseCopy[value.phase] || (/[\u4e00-\u9fff]/.test(value.phase) ? value.phase : '正在处理本机连接') : state.setup?.stage === 'starting' ? '等待本机采集就绪' : '等待本机状态更新';
    for (const prefix of ['progress', 'starting']) {
      $(prefix + '-phase').textContent = phase;
      $(prefix + '-count').textContent = detail;
      $(prefix + '-elapsed').textContent = seconds === null ? '' : String(Math.floor(seconds / 60)).padStart(2, '0') + ':' + String(seconds % 60).padStart(2, '0');
    }
  }

  function render() {
    const setup = state.setup;
    const stage = setup?.stage;
    const initialization = setup?.initialization || {};
    const active = state.connected;
    const action = initialization.state === 'needs_user_action' ? actionCopy[initialization.action] : null;
    const isWorking = working();
    $('connection-notice').hidden = !state.error;
    const expired = state.error?.status === 401;
    const fileMode = !allowedOrigin();
    $('connection-title').textContent = fileMode ? '请从本机启动器打开连接页面' : expired ? '本机连接会话已失效' : '暂时无法连接本机服务';
    $('connection-description').textContent = fileMode ? '这个页面需要本机连接服务。请运行 ChatLens 启动器，使用它自动打开的页面。' : expired ? '请回到本机 ChatLens 启动器，重新打开它提供的连接页面。' : '页面保留你已经选择的内容。请确认本机启动器仍在运行，然后重新连接。';
    $('retry-connection').disabled = expired || fileMode;
    $('retry-connection').textContent = expired || fileMode ? '请从启动器重新打开' : '重新连接';
    $('account-title').textContent = setup?.account?.detected ? setup.account.display_name?.trim() || '当前登录微信已识别' : setup ? '等待本机微信登录' : '正在识别当前登录的微信';
    $('account-description').textContent = setup?.account?.detected ? '将为当前登录的微信建立本机工作台。' : '请在这台电脑打开微信并完成登录，页面会自动检查。';
    const stageCopy = { needs_initialization: isWorking ? '正在初始化' : action ? '等待本人操作' : '需要首次连接', ready_to_select: '可以选择会话', starting: '正在启动采集', running: '工作台已就绪', error: '需要处理' };
    $('setup-stage').textContent = state.error ? '连接中断' : !active ? '正在连接' : stageCopy[stage] || '正在检查';
    $('setup-stage').className = 'tag tag-' + (state.error || stage === 'error' ? 'failure' : stage === 'ready_to_select' || stage === 'running' ? 'success' : isWorking ? 'running' : 'waiting');
    for (const [key, field] of [['wechat', 'wechat_running'], ['access', 'access_ready'], ['reader', 'reader_ready']]) {
      const value = setup?.checks?.[field];
      $(key + '-check').textContent = value === true ? '已就绪' : value === false ? '需要处理' : '检查中';
      $(key + '-check-dot').className = 'check-dot' + (value === true ? ' is-ready' : value === false ? ' is-missing' : '');
    }
    const step = !setup?.account?.detected ? 'account' : stage === 'ready_to_select' || stage === 'starting' || stage === 'running' ? 'select' : 'initialize';
    const order = ['account', 'initialize', 'select'];
    document.querySelectorAll('[data-step]').forEach(item => {
      if (item.dataset.step === step) item.setAttribute('aria-current', 'step'); else item.removeAttribute('aria-current');
      item.classList.toggle('is-complete', order.indexOf(item.dataset.step) < order.indexOf(step));
    });
    $('initialization-card').hidden = stage === 'ready_to_select' || stage === 'starting' || stage === 'running' || stage === 'error';
    $('initialization-progress').hidden = !isWorking || stage === 'starting';
    $('user-action-note').hidden = !action;
    if (action) {
      $('user-action-title').textContent = action[0];
      $('user-action-description').textContent = typeof initialization.description === 'string' && initialization.description.trim() ? initialization.description : action[1];
    }
    $('initialize-button').disabled = !active || Boolean(state.busy) || initialization.state === 'working' || (!setup?.account?.detected && !action);
    $('initialize-button').classList.toggle('is-loading', initialization.state === 'working' || state.busy === 'initialize');
    $('initialize-button').setAttribute('aria-busy', String(initialization.state === 'working' || state.busy === 'initialize'));
    $('initialize-button').textContent = !active ? '等待本机连接' : isWorking ? '正在初始化' : action ? action[2] : setup?.account?.detected ? '开始本机初始化' : '等待微信登录';
    $('initialize-help').textContent = action ? '请本人完成上方操作，页面也会自动继续检查。' : isWorking ? '正在进行本机初始化，请按页面出现的提示操作。' : '不会自动退出你的微信；需要本人操作时会明确提示。';
    $('setup-error').hidden = stage !== 'error';
    $('setup-error-description').textContent = typeof initialization.description === 'string' && initialization.description.trim() ? initialization.description : '本机连接没有完成。请确认微信状态和文件访问权限，再尝试初始化。';
    $('setup-reason').textContent = /^[a-z0-9_]{1,100}$/.test(setup?.reason || '') ? setup.reason : '暂未提供其他状态信息。';
    $('retry-initialization').disabled = !active || isWorking || !setup?.account?.detected;
    $('check-status').disabled = Boolean(state.busy);
    $('selection-card').hidden = stage !== 'ready_to_select';
    $('starting-card').hidden = stage !== 'starting' && stage !== 'running';
    $('starting-title').textContent = stage === 'running' ? '工作台已就绪' : '正在启动采集';
    $('starting-description').textContent = typeof initialization.description === 'string' && initialization.description.trim() ? initialization.description : '正在确认所选会话并建立新消息起点。请切回此页面，让微信保持后台，等待状态更新。';
    $('starting-button').textContent = stage === 'running' ? '正在打开工作台' : '正在启动采集';
    $('setup-conversation-search').disabled = !canSelect();
    progress();
    renderSelection();
  }

  function pickerEmpty(title, description) {
    const result = node('div', 'picker-empty');
    result.append(node('strong', '', title), node('p', '', description));
    return result;
  }

  function renderSelection() {
    for (const type of ['private', 'group']) {
      const list = $(type + '-list');
      list.setAttribute('aria-busy', String(state.listLoading));
      const rows = state.rows.filter(row => row.type === type);
      $(type + '-result-count').textContent = state.listLoading ? '读取中' : String(rows.length);
      const signature = JSON.stringify([rows, state.selected[type]?.id, canSelect(), state.listLoading, state.listError]);
      if (renderedLists.get(type) !== signature) {
        const focused = list.contains(document.activeElement) ? document.activeElement.dataset.pickerId : null;
        list.replaceChildren();
        for (const row of rows) {
        const selected = state.selected[type]?.id === row.id;
        const button = node('button', 'picker-row');
        button.type = 'button';
        button.setAttribute('role', 'radio');
        button.setAttribute('aria-checked', String(selected));
        button.dataset.pickerId = row.id;
        button.disabled = !canSelect() || state.listLoading || state.listError;
        button.append(node('span', 'avatar ' + (type === 'group' ? 'sand' : 'sage'), row.name.slice(0, type === 'group' ? 2 : 1)), node('strong', '', row.name), node('span', 'picker-radio'));
        button.addEventListener('click', () => { if (canSelect()) { state.selected[type] = row; renderSelection(); $('setup-announcer').textContent = '已选择' + (type === 'group' ? '群聊' : '私聊') + '。'; } });
        list.append(button);
        }
        if (!rows.length) list.append(pickerEmpty(state.listLoading ? '正在读取会话名称' : state.listError ? '暂时无法读取会话' : '没有匹配的' + (type === 'group' ? '群聊' : '私聊'), state.listLoading ? '只读取当前账号的会话名称。' : state.listError ? '请重新读取，已选内容会保留。' : '试试其他名称，或清空搜索。'));
        renderedLists.set(type, signature);
        if (focused) [...list.children].find(button => button.dataset.pickerId === focused)?.focus({ preventScroll: true });
      }
      $('selected-' + type).textContent = state.selected[type]?.name || '尚未选择';
    }
    const total = Number(Boolean(state.selected.private)) + Number(Boolean(state.selected.group));
    $('selection-count').textContent = '已选择 ' + total + ' / 2';
    $('start-selected').disabled = total !== 2 || !canSelect() || state.listLoading || state.listError;
    $('start-selected').classList.toggle('is-loading', state.busy === 'select');
    $('start-selected').setAttribute('aria-busy', String(state.busy === 'select'));
    if (state.busy === 'select') $('start-selected').textContent = '正在确认所选会话';
    else $('start-selected').textContent = '开始关注这些会话';
    $('conversation-feedback').hidden = !state.listLoading && !state.listError && !state.listMore;
    $('conversation-feedback-text').textContent = state.listLoading ? '正在查找会话名称…' : state.listError ? '读取没有完成，请重新尝试。' : '只展示部分匹配结果，请输入更具体的名称继续查找。';
    $('retry-conversations').hidden = !state.listError;
    $('retry-conversations').disabled = !canSelect();
  }

  async function loadConversations() {
    if (!canSelect()) return;
    const request = ++state.listRequest;
    const query = $('setup-conversation-search').value.trim();
    state.listLoading = true;
    state.listError = false;
    renderSelection();
    try {
      const result = await api('/api/setup/conversations?' + new URLSearchParams({ q: query }));
      if (request !== state.listRequest || state.setup?.stage !== 'ready_to_select') return;
      if (!Array.isArray(result?.conversations)) throw error('invalid_response');
      const seen = new Set();
      const rows = result.conversations.map(row => {
        if (!row || typeof row.id !== 'string' || !row.id || seen.has(row.id) || typeof row.name !== 'string' || !row.name.trim() || !['private', 'group'].includes(row.type)) throw error('invalid_response');
        seen.add(row.id);
        return { id: row.id, name: row.name, type: row.type };
      });
      state.rows = rows;
      state.listMore = result.has_more === true;
    } catch (problem) {
      if (request === state.listRequest) {
        state.listError = true;
        if (problem.status === 401) setError(problem);
      }
    } finally { if (request === state.listRequest) { state.listLoading = false; renderSelection(); } }
  }

  async function mutate(kind) {
    if (!state.connected || working()) return;
    if (kind === 'select' && (!canSelect() || !state.selected.private || !state.selected.group)) return;
    state.busy = kind;
    state.setupRequest++;
    render();
    try {
      const body = kind === 'select' ? { private_id: state.selected.private.id, group_id: state.selected.group.id } : {};
      const result = await api('/api/setup/' + (kind === 'select' ? 'select' : 'initialize'), { method: 'POST', body });
      if (stages.has(result?.stage) && typeof result.account?.detected === 'boolean') acceptSetup(result);
      else acceptSetup(await api('/api/setup'));
    } catch (problem) { setError(problem); }
    finally {
      state.busy = null;
      render();
      if (state.setup?.stage === 'ready_to_select' && !state.listLoading && !state.rows.length) loadConversations();
      schedule(state.epoch);
    }
  }

  $('initialize-button').addEventListener('click', () => mutate('initialize'));
  $('retry-initialization').addEventListener('click', () => mutate('initialize'));
  $('start-selected').addEventListener('click', () => mutate('select'));
  $('retry-connection').addEventListener('click', connect);
  $('check-status').addEventListener('click', () => poll());
  $('retry-conversations').addEventListener('click', loadConversations);
  for (const type of ['private', 'group']) $(type + '-list').addEventListener('keydown', event => {
    if (!['ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    const rows = [...$(type + '-list').querySelectorAll('.picker-row:not(:disabled)')];
    if (!rows.length) return;
    const current = rows.indexOf(document.activeElement);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? rows.length - 1 : (current + (['ArrowUp', 'ArrowLeft'].includes(event.key) ? -1 : 1) + rows.length) % rows.length;
    event.preventDefault();
    rows[next].focus();
    rows[next].click();
  });
  $('setup-conversation-search').addEventListener('input', () => {
    clearTimeout(searchTimer);
    state.listRequest++;
    searchTimer = setTimeout(loadConversations, 250);
  });
  window.addEventListener('pageshow', event => { if (event.persisted) connect(); });
  if (allowedOrigin()) connect();
  else { setError(error('loopback_required')); $('setup-announcer').textContent = '请从本机启动器打开连接页面。'; }
})();
