/* Local setup and isolated focus scopes. Only names and public state enter this page. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const stages = new Set(['needs_initialization', 'ready_to_select', 'ready_to_start', 'starting', 'running', 'error', 'waiting_for_wechat', 'needs_connection']);
  const connectionStates = new Set(['waiting_for_wechat', 'checking_account', 'account_unverified', 'wrong_account', 'connected', 'stopped', 'explicit_read_only', 'cleanup_required']);
  const state = { setup: null, platform: null, generation: null, connected: false, error: null, busy: null, epoch: 0, setupRequest: 0, timer: null, failures: 0, listRequest: 0, listLoading: false, listError: false, catalogError: null, listMore: false, rows: [], selected: new Map(), selectionSeeded: false, pickerGeneration: null, loadedGeneration: null, editing: false, redirecting: false, recovering: false, enterWorkspace: false, stayOnSetup: new URLSearchParams(location.search).get('manage') === '1' };
  const renderedLists = new Map();
  let profilesSignature = '';
  let searchTimer;
  let toastTimer;

  function node(tag, className, text) { const result = document.createElement(tag); if (className) result.className = className; if (text !== undefined) result.textContent = text; return result; }
  function feedback(text) { clearTimeout(toastTimer); $('setup-toast').textContent = text; $('setup-toast').hidden = false; toastTimer = setTimeout(() => { $('setup-toast').hidden = true; }, 4200); }
  function error(code, status = 0) { return Object.assign(new Error(), { code, status }); }
  function ignored(problem) { return ['stale_response', 'scope_changed'].includes(problem?.code); }
  function allowedOrigin() { return location.protocol === 'http:' && location.hostname === '127.0.0.1'; }
  function validGeneration(value) { return typeof value === 'string' && /^[a-zA-Z0-9_-]{16,128}$/.test(value); }
  function windows() { return state.platform === 'windows' || state.setup?.platform === 'windows'; }
  function connection() { return windows() ? state.setup?.connection : null; }
  function collectorStage(value = connection()) { return typeof value?.collector_stage === 'string' ? value.collector_stage : null; }
  function collectorRunning(value = connection()) { return typeof value?.collector_running === 'boolean' ? value.collector_running : value?.checks?.collecting === true; }
  function collectorReady(value = connection()) { const stage = collectorStage(value); return value?.can_read_saved === true && collectorRunning(value) && (stage === null ? value?.checks?.collecting === true : ['collecting', 'paused'].includes(stage)); }
  function canOpenWorkspace() { return connection()?.can_read_saved === true && (!collectorRunning() || collectorReady()); }
  function connectionAction(kind) { const value = connection(); return value?.['can_' + kind] === true && Array.isArray(value.actions) && value.actions.includes(kind); }
  function working() { return state.busy !== null || state.setup?.initialization?.state === 'working' || state.setup?.stage === 'starting' || state.setup?.scope_switching === true || connection()?.state === 'checking_account' || collectorRunning() && collectorStage() === 'starting'; }
  function catalogStatus() {
    if (state.catalogError) return 'error';
    const value = state.setup?.catalog?.state;
    if (['loading', 'ready', 'error'].includes(value)) return value;
    if (['windows_scope_catalog_unavailable', 'macos_contact_catalog_unavailable'].includes(state.setup?.reason)) return 'error';
    if (value === 'idle') return 'idle';
    return state.setup?.stage === 'ready_to_select' ? 'ready' : 'idle';
  }
  function catalogReady() { return catalogStatus() === 'ready'; }
  function canManageSelection() { return state.connected && Boolean(state.generation) && (windows() ? connection()?.can_modify_scope === true : ['ready_to_select', 'running', 'error'].includes(state.setup?.stage)) && !working(); }
  function canSelect() { return canManageSelection() && state.editing && catalogReady(); }
  function scopeLimit() { const value = state.setup?.scope_limits?.total; return Number.isSafeInteger(value) && value > 0 ? Math.min(value, 50) : 50; }
  function resetPicker() { state.listRequest++; state.rows = []; state.selected.clear(); state.selectionSeeded = false; state.pickerGeneration = null; state.loadedGeneration = null; state.listLoading = false; state.listError = false; state.catalogError = null; state.listMore = false; state.editing = false; clearTimeout(searchTimer); $('setup-conversation-search').value = ''; renderedLists.clear(); }

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
      if (typeof result?.scope_generation === 'string' && result.scope_generation !== generation && !['/api/session', '/api/setup', '/api/setup/select', '/api/setup/profile', '/api/setup/recheck', '/api/setup/start'].includes(route)) { recoverScope(); throw error('scope_changed', 409); }
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
    if (value.platform === 'windows' && value.connection && (!connectionStates.has(value.connection.state) || typeof value.connection.can_read_saved !== 'boolean' || typeof value.connection.can_modify_scope !== 'boolean')) throw error('invalid_response');
    const nextGeneration = value.scope_generation || state.generation;
    const changed = nextGeneration !== state.generation;
    if (changed) { resetPicker(); if (!state.enterWorkspace) state.stayOnSetup = true; }
    state.generation = nextGeneration;
    if (typeof value.platform === 'string') state.platform = value.platform;
    state.setup = value;
    state.connected = true;
    state.error = null;
    state.failures = 0;
    if (value.stage === 'ready_to_select' && !value.scope_switching) state.editing = true;
    if (value.stage === 'starting' || value.scope_switching) { state.editing = false; state.listRequest++; state.listLoading = false; }
    render();
    if (canSelect() && state.loadedGeneration !== state.generation && !state.listLoading && !state.listError) loadConversations();
    if ((windows() ? state.enterWorkspace && collectorReady() : value.stage === 'running' && (state.enterWorkspace || !state.stayOnSetup)) && !state.redirecting) openWorkspace();
  }
  function openWorkspace() { if (!state.connected || (windows() ? !canOpenWorkspace() : state.setup?.stage !== 'running') || state.setup?.scope_switching || state.redirecting) return; state.redirecting = true; const target = ['/', '/index.html'].includes(state.setup.workspace_path) ? state.setup.workspace_path : '/'; location.assign(target); }
  function schedule(epoch) { clearTimeout(state.timer); if (state.error?.status === 401 || !allowedOrigin() || epoch !== state.epoch || state.redirecting) return; state.timer = setTimeout(() => poll(epoch), state.failures ? Math.min(10000, 1000 * 2 ** Math.min(state.failures, 4)) : 1000); }

  async function connect() {
    const epoch = ++state.epoch, request = ++state.setupRequest;
    clearTimeout(state.timer); state.error = null; state.connected = false; render();
    try {
      let token = new URLSearchParams(location.hash.slice(1)).get('token');
      const session = await api('/api/session', { token }); token = null;
      if (epoch !== state.epoch) return;
      const generation = validGeneration(session?.scope_generation) ? session.scope_generation : null;
      if (typeof session?.platform === 'string') state.platform = session.platform;
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

  function windowsCopy(value = {}) {
    if (value.state === 'account_unverified') return ['账号读取核验未完成', 'waiting', '读取待核验', value.reason === 'windows_reader_configuration_unavailable' ? '请保持当前已登录状态，并确认选择的是此前测试成功的完整资料目录，再重新核验读取配置。原数据和检查点保留。' : '如果微信已登录，请保持当前状态，点击“重新检测微信”重新核验读取依据。仍未完成时，点击“导出连接诊断”提供定位信息，不需要反复退出或重新登录。'];
    const phase = collectorStage(value);
    if (collectorRunning(value) && ['connected', 'stopped'].includes(value.state) && phase !== null) {
      const phases = {
        starting: ['正在准备采集', 'running', '等待本轮状态', '采集进程已经启动，正在等待本轮采集状态确认。请保持页面打开，不要重复点击开始；原消息和检查点保留。'],
        collecting: ['正在采集所选会话', 'running', '本轮采集中', '本轮采集状态已确认，可以进入工作台查看新文字。显示内容以本机实际保存的消息为准。'],
        paused: ['采集暂时暂停', 'waiting', '等待读取条件', '本轮采集仍在运行，目前处于暂停阶段。若微信在前台或屏幕锁定，请让微信保持后台并解锁屏幕，再等待状态恢复，不需要重复开始。'],
        stale: ['等待本轮采集心跳', 'waiting', '状态暂未确认', '暂未收到本轮的新鲜采集心跳，不能据此认定已经停止。原消息和进度保留，当前不会重复启动另一轮采集。'],
        error: ['本轮采集需要处理', 'failure', '采集异常', '本轮采集报告异常。请保留原窗口、数据和检查点，按实际状态原因处理；进程结束前不会重复开始采集。'],
        stopping: ['正在结束采集', 'waiting', '正在保存进度', '采集仍在结束和保存进度，请等待本轮进程正常退出后再继续。'],
        stopped: ['正在确认采集退出', 'waiting', '等待正常结束', '采集已报告停止，正在等待本轮进程确认退出。请保留页面，不要重复开始。']
      };
      return phases[phase] || phases.stale;
    }
    if (phase === 'error' && value.state === 'stopped') return ['上一轮采集异常结束', 'failure', '需要检查', '原消息和检查点仍在。请查看本轮实际原因，重新检测原账号和读取条件后，再开始采集。'];
    if (value.checks?.collecting === true) return ['采集已启动', 'running', '采集进程运行中', '可以进入工作台查看实际采集阶段和新文字，或修改关注范围。微信在前台等情况会暂停读取，请以工作台状态为准。'];
    if (value.reason === 'windows_reader_configuration_unavailable') return ['读取配置需要检查', 'waiting', '配置未就绪', '原账号的读取范围已核验，但已有配置未通过检查。请保留原资料，确认选择的是此前测试成功的完整 collector-data，再重新检测。'];
    const copies = {
      waiting_for_wechat: ['等待本机微信', 'waiting', '未找到微信进程', '请打开这台电脑上的微信，并由你完成扫码或手机登录确认，再点击“重新检测微信”。本工具不会替你登录。'],
      checking_account: ['正在检查微信与账号', 'running', '正在核验', '正在检查微信程序和原账号的本机读取范围。请等待实际状态返回，不需要退出或重启本工具。'],
      wrong_account: ['当前微信与原资料账号不匹配', 'failure', '账号不匹配', '请在微信中切回这份资料对应的原账号，再点击“重新检测微信”。已停止原采集，不会把两个账号的消息或配置混在一起。'],
      connected: ['原账号读取已核验', 'success', '可以开始采集', '可以继续使用原关注范围，也可以先修改关注范围。点击“开始采集”后才接收新的文字，原消息和采集位置会保留。'],
      stopped: ['采集已停止，可以继续', 'waiting', '等待开始', '原消息和采集位置仍在。确认原账号的读取范围已核验后，点击“开始采集”继续；需要重新核验时可点击“重新检测微信”。'],
      explicit_read_only: ['当前是离线只读模式', 'neutral', '仅查看已保存内容', '这个入口明确选择了离线只读，不检测微信、不开始采集，也不能修改关注范围。你可以查看本机已保存消息；需要连接微信时请使用标准启动入口。'],
      cleanup_required: ['等待上一轮采集结束', 'waiting', '暂不能开始', '上一轮采集尚未确认退出。请保留原窗口和资料，等待其正常结束后重新检测；不要删除锁、配置或检查点，也不要同时启动另一轮采集。']
    };
    return copies[value.state] || ['等待本机连接状态', 'waiting', '等待检查', '请等待本机服务返回实际连接状态。'];
  }

  function renderWindows(setup, active, isWorking) {
    const value = connection() || {}, facts = value.checks || {}, copy = windowsCopy(value), phase = collectorStage(value), collectorAlive = collectorRunning(value), ready = collectorReady(value);
    $('windows-connection-card').hidden = false;
    $('windows-sidebar-steps').hidden = false;
    $('setup-steps').hidden = true;
    $('mac-account-card').hidden = true;
    $('initialization-card').hidden = true;
    $('setup-error').hidden = true;
    document.querySelector('.connect-heading h1').textContent = '连接微信，开始关注。';
    document.querySelector('.connect-heading p').textContent = '按实际连接状态继续操作，原消息、配置和采集位置保留。';
    $('windows-connection-title').textContent = value.state === 'connected' && phase === null && (state.busy === 'start' || setup?.stage === 'starting' && setup?.scope_switching !== true) ? '正在准备所选范围的采集' : copy[0];
    $('windows-connection-state').textContent = copy[2];
    $('windows-connection-state').className = 'tag tag-' + copy[1];
    const unverifiedDescription = value.reason === 'windows_reader_configuration_unavailable' ? '原账号的读取依据已确认，但读取配置尚未通过检查。当前需要处理的是读取配置，不是微信登录状态。' : facts.process_running === true ? '微信程序已找到，但尚未定位到这份资料对应的账号读取依据。这属于读取定位或核验未完成，不代表微信未登录。' : '微信程序的身份或访问条件尚未完成核验，暂不能定位账号读取依据。这里不会据此判断账号是否登录。';
    $('windows-connection-description').textContent = value.state === 'account_unverified' ? unverifiedDescription : typeof value.description === 'string' && value.description.trim() ? value.description.slice(0, 1500) : '先检测微信程序，再核验原账号的本机读取范围；程序存在不代表已完成登录或网络在线。';
    $('windows-next-step').textContent = copy[3];
    const keys = ['process_running', 'account_verified', 'reader_ready', 'scope_selected', 'collecting'];
    const selectedCount = count(setup?.active_scope?.count);
    const details = {
      process_running: facts.process_running === true ? ['进程已找到', '已找到这台电脑上的微信程序'] : facts.process_running === false ? ['未找到', '请打开本机微信后重新检测'] : ['尚未检查', '等待本机进程检查结果'],
      account_verified: facts.account_verified === true ? ['读取已核验', '与这份资料对应的原账号一致'] : value.state === 'wrong_account' ? ['账号不匹配', '请切回原账号再重新检测'] : ['尚未核验', '等待本机账号读取依据核验'],
      reader_ready: facts.reader_ready === true ? ['配置可用', '复用原有读取配置和检查点'] : value.reason === 'windows_reader_configuration_unavailable' ? ['检查未通过', '请确认所选完整资料目录'] : ['待核验', '保留原资料目录，等待配置核验'],
      scope_selected: facts.scope_selected === true ? ['已有范围', selectedCount === null ? '已保存你选择的关注范围' : '已保存 ' + selectedCount + ' 个关注会话'] : ['待选择', '读取就绪后选择私聊和群聊'],
      collecting: collectorAlive && phase === 'starting' ? ['准备中', '等待本轮采集状态确认'] : collectorAlive && phase === 'paused' ? ['已暂停', '等待后台、解锁等读取条件'] : collectorAlive && phase === 'stale' ? ['等待心跳', '不将状态未知当作已经停止'] : collectorAlive && phase === 'error' ? ['需要处理', '保留进度，查看实际状态原因'] : collectorAlive && ['stopping', 'stopped'].includes(phase) ? ['正在结束', '等待本轮进程正常退出'] : facts.collecting === true ? [phase === null ? '进程运行中' : '正在采集', '查看工作台中的实际采集阶段'] : value.state === 'checking_account' ? ['尚未开始', '账号和配置核验通过后再开始'] : ['未在采集', value.state === 'explicit_read_only' ? '离线只读，不接收新的消息' : '点击“开始采集”后才继续接收']
    };
    let current = keys.find(key => facts[key] !== true);
    for (const key of keys) {
      const item = document.querySelector('[data-windows-check="' + key + '"]');
      item.querySelector('.windows-check-state').textContent = details[key][0];
      item.querySelector('.windows-check-detail').textContent = details[key][1];
      item.classList.toggle('is-complete', facts[key] === true);
      item.classList.toggle('is-current', key === current && value.state !== 'explicit_read_only');
      const sidebar = document.querySelector('[data-windows-sidebar-step="' + key + '"]');
      sidebar.classList.toggle('is-complete', facts[key] === true);
      if (key === current) sidebar.setAttribute('aria-current', 'step'); else sidebar.removeAttribute('aria-current');
    }
    const recheck = $('windows-recheck'), start = $('windows-start');
    recheck.hidden = value.state === 'explicit_read_only';
    recheck.disabled = !active || Boolean(state.busy) || !connectionAction('recheck');
    recheck.classList.toggle('button-primary', value.can_start !== true);
    recheck.textContent = state.busy === 'recheck' || value.state === 'checking_account' ? '正在检测微信' : '重新检测微信';
    recheck.classList.toggle('is-loading', state.busy === 'recheck' || value.state === 'checking_account');
    recheck.setAttribute('aria-busy', String(state.busy === 'recheck' || value.state === 'checking_account'));
    start.hidden = collectorAlive || !value.can_start && !['connected', 'stopped'].includes(value.state);
    start.disabled = !active || collectorAlive || Boolean(state.busy) || !connectionAction('start');
    start.textContent = state.busy === 'start' ? '正在开始采集' : '开始采集';
    start.classList.toggle('is-loading', state.busy === 'start');
    start.setAttribute('aria-busy', String(state.busy === 'start'));
    const diagnostics = $('download-connection-diagnostics');
    diagnostics.disabled = !active || !state.generation || Boolean(state.busy);
    diagnostics.textContent = state.busy === 'diagnostics' ? '正在导出诊断' : '导出连接诊断';
    diagnostics.classList.toggle('is-loading', state.busy === 'diagnostics');
    diagnostics.setAttribute('aria-busy', String(state.busy === 'diagnostics'));
    $('windows-offline').hidden = value.can_read_saved !== true || collectorAlive;
    $('windows-open-workspace').hidden = !ready;
    $('windows-open-workspace').textContent = phase === 'paused' ? '查看工作台（采集暂停）' : '进入工作台';
    $('windows-offline').disabled = $('windows-open-workspace').disabled = !active || setup?.scope_switching === true;
    $('windows-saved-note').textContent = value.can_read_saved === true ? collectorAlive ? phase === 'starting' ? '正在等待本轮采集状态，不沿用旧一轮的停止或就绪记录。原数据、配置和采集位置保留。' : '采集进程仍在运行，当前阶段请以上方本轮状态为准。账号读取核验不代表网络在线或已有新来信。' : '现在可以离线查看已保存的消息；这不表示正在采集。原消息、模型配置和检查点保留。' : '原资料保留在本机。读取范围未核验时不会显示其他账号的消息，也不会重建原基线。';
    $('windows-connection-reason').textContent = /^[a-z0-9_]{1,100}$/.test(value.reason || '') ? '状态原因：' + value.reason : '暂无其他状态原因。';
    $('scope-capability-note').hidden = !active || Boolean(state.generation);
    $('scope-manager').hidden = !state.generation;
    $('active-scope-label').textContent = typeof setup?.active_scope?.label === 'string' ? setup.active_scope.label : '尚未读取关注范围';
    const scope = setup?.active_scope;
    $('active-scope-count').textContent = scope && selectedCount !== null ? selectedCount + ' 个会话 · ' + (count(scope.private_count) || 0) + ' 个私聊 · ' + (count(scope.group_count) || 0) + ' 个群聊' : '完成原账号读取核验后，可以查看和修改关注范围。';
    $('scope-manager-description').textContent = collectorAlive ? '查看和搜索名称时，当前采集进程保持运行。确认启用新的范围后才会切换。' : '可以保留原关注范围。修改范围会独立保存消息、AI 历史和采集位置，原范围仍可切回。';
    const canManage = active && value.can_manage === true && value.can_modify_scope === true && !isWorking;
    $('manage-conversations').disabled = !canManage || setup?.catalog?.worker_active === true;
    $('manage-conversations').textContent = setup?.catalog?.state === 'loading' ? '正在读取会话名称' : state.editing ? '查看会话选择' : '修改关注范围';
    $('manage-conversations').classList.toggle('is-loading', setup?.catalog?.state === 'loading');
    $('scope-manage-help').hidden = canManage && setup?.catalog?.worker_active !== true;
    $('scope-manage-help').textContent = setup?.catalog?.worker_active === true ? '本次会话目录读取仍在处理或清理，请等待实际状态更新，不会重复启动另一轮读取。' : value.state === 'explicit_read_only' ? '离线只读模式不能修改关注范围。原范围与已保存消息保持不变。' : value.state === 'cleanup_required' ? '上一轮采集尚未确认停止，暂不能修改范围。请先按上方说明处理。' : isWorking ? '当前步骤完成后才能修改范围，请等待上方实际状态更新。' : '需要先完成原账号及读取配置核验，才能加载和修改会话。请使用上方“重新检测微信”继续。';
    $('open-workspace').disabled = !active || !canOpenWorkspace() || setup?.scope_switching === true;
    $('open-workspace').textContent = collectorAlive ? ready ? phase === 'paused' ? '查看工作台（采集暂停）' : '返回当前工作台' : '等待本轮采集状态' : '查看已保存消息（离线）';
    $('selection-card').hidden = !state.editing || !state.generation || value.can_modify_scope !== true || setup?.scope_switching === true;
    $('selection-limit').textContent = '可选 1–' + scopeLimit() + ' 个会话';
    $('starting-card').hidden = setup?.scope_switching !== true;
    $('starting-title').textContent = '正在切换关注范围';
    $('starting-description').textContent = '正在停止上一范围并恢复所选范围，各自的消息和采集位置分别保留。';
    progress(); renderSelection(); renderProfiles();
  }

  function render() {
    const setup = state.setup, stage = setup?.stage, initialization = setup?.initialization || {}, active = state.connected, action = initialization.state === 'needs_user_action' ? actionCopy[initialization.action] : null, isWorking = working();
    $('connection-notice').hidden = !state.error;
    const expired = state.error?.status === 401, fileMode = !allowedOrigin(), scopeChanged = state.error?.code === 'scope_changed';
    $('connection-title').textContent = fileMode ? '请从本机启动器打开连接页面' : expired ? '本机连接会话已失效' : scopeChanged ? '关注范围已变化' : '暂时无法连接本机服务';
    $('connection-description').textContent = fileMode ? '这个页面需要本机连接服务。请运行 ChatLens 启动器，使用它自动打开的页面。' : expired ? '请回到本机 ChatLens 启动器，重新打开它提供的连接页面。' : scopeChanged ? '旧选择已清除，正在重新读取当前关注范围。不会重试之前的切换操作。' : '页面保留当前范围的选择。请确认本机启动器仍在运行，然后重新连接。';
    $('retry-connection').disabled = expired || fileMode;
    $('retry-connection').textContent = expired || fileMode ? '请从启动器重新打开' : '重新连接';
    if (windows()) { renderWindows(setup, active, isWorking); return; }
    $('windows-connection-card').hidden = true;
    $('windows-sidebar-steps').hidden = true;
    $('setup-steps').hidden = false;
    $('mac-account-card').hidden = false;
    $('scope-manage-help').hidden = true;
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
    $('manage-conversations').disabled = !active || isWorking || setup?.checks?.reader_ready !== true || setup?.catalog?.worker_active === true;
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
  function catalogProgress() {
    const catalog = state.setup?.catalog || {};
    const phases = { queued: '等待读取会话目录', validating_sources: '正在检查读取资料', opening_message: '正在打开消息库', reading_message_tables: '正在查找会话索引', opening_contacts: '正在打开联系人库', reading_contacts: '正在读取联系人名称', preparing_choices: '正在整理可选会话' };
    const parts = [phases[catalog.phase] || '正在准备会话目录'];
    const seconds = count(catalog.elapsed_seconds);
    if (seconds !== null) parts.push('用时 ' + String(Math.floor(seconds / 60)).padStart(2, '0') + ':' + String(seconds % 60).padStart(2, '0'));
    if (count(catalog.databases_checked) > 0) parts.push('已检查 ' + catalog.databases_checked + ' 个库');
    if (count(catalog.contacts_seen) > 0) parts.push('已读取 ' + catalog.contacts_seen + ' 条联系人');
    return parts.join(' · ');
  }
  function pickerPhase() {
    if (!state.connected) return ['blocked', '等待本机连接', '请先恢复本机服务连接，当前选择会保留。'];
    if (state.busy === 'catalog' || catalogStatus() === 'loading') return ['loading', '正在准备会话目录', catalogProgress() + '。原关注范围保持不变。'];
    if (!canManageSelection()) return ['blocked', '暂时无法修改关注范围', windows() ? $('scope-manage-help').textContent : '请等待当前步骤结束，并确认微信账号与本机读取能力已就绪。'];
    if (catalogStatus() === 'error') {
      const reason = state.catalogError || state.setup?.catalog?.reason || state.setup?.reason;
      const descriptions = { windows_scope_catalog_timeout: '会话目录读取超时，原消息和关注范围保留。', windows_scope_catalog_limit: '会话目录超过当前读取上限，原关注范围保留。', windows_scope_key_authentication_failed: '会话目录读取配置未通过校验，原配置和消息保留。', windows_scope_catalog_source_changed: '读取期间资料发生变化，原关注范围保留。', windows_scope_catalog_cancelled: '本次会话目录读取已取消，原关注范围保留。' };
      return ['error', '会话目录读取未完成', (descriptions[reason] || '本次未能读取会话目录，原消息和关注范围保留。') + (state.setup?.catalog?.worker_active === true ? '正在结束本次读取，请稍候再重试。' : '请点击“重新读取会话目录”重试。')];
    }
    if (!catalogReady()) return ['idle', '尚未读取会话目录', '请点击“读取会话目录”，准备可选私聊和群聊。'];
    if (state.listLoading || !state.selectionSeeded && !state.listError) return ['loading', '正在加载会话名称', '只查询当前账号的名称，已选会话会保留。'];
    if (state.listError) return ['error', '会话列表加载未完成', '请点击“重新加载列表”，当前范围内的选择会保留。'];
    return ['ready', '', ''];
  }
  function renderSelection() {
    const phase = pickerPhase(), loading = phase[0] === 'loading', failed = phase[0] === 'error';
    $('setup-conversation-search').disabled = !canSelect() || !state.selectionSeeded;
    $('selection-card').dataset.catalogState = phase[0];
    for (const type of ['private', 'group']) {
      const list = $(type + '-list'), rows = phase[0] === 'ready' ? state.rows.filter(row => row.type === type) : [];
      list.setAttribute('aria-busy', String(loading));
      $(type + '-result-count').textContent = loading ? '读取中' : phase[0] === 'ready' ? String(rows.length) : '—';
      const signature = JSON.stringify([rows, [...state.selected.keys()], canSelect(), phase, state.listError, scopeLimit(), $('setup-conversation-search').value]);
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
        if (!rows.length) {
          const kind = type === 'group' ? '群聊' : '私聊', searched = Boolean($('setup-conversation-search').value.trim());
          list.append(pickerEmpty(phase[0] === 'ready' ? (searched ? '没有匹配的' : '暂无可选') + kind : phase[1], phase[0] === 'ready' ? searched ? '试试其他名称，或清空搜索。' : '当前目录中没有可选的' + kind + '。' : phase[2]));
        }
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
    $('conversation-feedback').hidden = phase[0] === 'ready' && !state.listMore;
    $('conversation-feedback').classList.toggle('is-error', failed);
    $('conversation-feedback').setAttribute('role', failed ? 'alert' : 'status');
    $('conversation-feedback-text').textContent = phase[0] === 'ready' ? '只展示部分匹配结果，请输入更具体的名称继续查找。' : phase[2];
    $('retry-conversations').hidden = !failed && phase[0] !== 'idle';
    $('retry-conversations').disabled = !canManageSelection() || state.setup?.catalog?.worker_active === true;
    $('retry-conversations').textContent = catalogReady() && state.listError ? '重新加载列表' : phase[0] === 'idle' ? '读取会话目录' : '重新读取会话目录';
  }
  function renderProfiles() {
    const rows = Array.isArray(state.setup?.profiles) ? state.setup.profiles.filter(row => typeof row?.id === 'string' && typeof row.label === 'string' && count(row.count) !== null) : [];
    $('profiles-card').hidden = !state.generation || !rows.length;
    const signature = JSON.stringify([state.generation, rows, state.connected, working(), connection()?.can_modify_scope]); if (signature === profilesSignature) return; profilesSignature = signature;
    const list = $('saved-profiles'); list.replaceChildren();
    for (const profile of rows) { const generation = state.generation, row = node('article', 'scope-profile' + (profile.active ? ' is-active' : '')); const copy = node('div'); copy.append(node('strong', '', profile.label), node('p', '', profile.count + ' 个会话 · ' + (count(profile.private_count) || 0) + ' 个私聊 · ' + (count(profile.group_count) || 0) + ' 个群聊')); const button = node('button', 'button', profile.active ? '当前使用' : '切回此范围'); button.type = 'button'; button.dataset.profileId = profile.id; button.disabled = profile.active || !state.connected || working() || windows() && connection()?.can_modify_scope !== true; button.addEventListener('click', () => { if (generation === state.generation) mutate('profile', profile.id); }); row.append(copy, button); list.append(row); }
  }

  async function loadConversations() {
    if (!canSelect()) return;
    const request = ++state.listRequest, generation = state.generation, query = state.selectionSeeded ? $('setup-conversation-search').value.trim() : ''; state.listLoading = true; state.listError = false; renderSelection();
    try {
      const result = await api('/api/setup/conversations?' + new URLSearchParams({ q: query }));
      if (request !== state.listRequest || generation !== state.generation || !canSelect()) return;
      if (result?.catalog && ['idle', 'loading', 'error'].includes(result.catalog.state)) {
        state.setup = { ...state.setup, catalog: result.catalog };
        state.loadedGeneration = null;
        return;
      }
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

  function connectionDiagnostics(value) {
    const top = ['schema_version', 'kind', 'platform', 'connection_state', 'account_reason', 'checks', 'account_diagnostics', 'selected_conversation_count', 'collector_running', 'collector_stage'];
    const object = item => item !== null && typeof item === 'object' && !Array.isArray(item);
    const keys = (item, allowed) => object(item) && Object.keys(item).length === allowed.length && allowed.every(key => Object.hasOwn(item, key));
    const code = item => item === null || typeof item === 'string' && /^[a-z0-9_]{1,100}$/.test(item);
    const checks = ['process_running', 'account_verified', 'reader_ready', 'scope_selected', 'collecting'];
    const counters = ['handles_seen', 'handles_with_name', 'type_names_present', 'file_type_handles', 'file_names_retained', 'all_named_db_candidates', 'database_names_seen', 'matching_root_paths', 'other_root_paths', 'unresolved_database_paths', 'family_process_count', 'family_processes_completed', 'main_database_names_seen', 'helper_database_names_seen', 'mapped_verification_passes', 'mapped_regions_queried', 'mapped_data_regions_seen', 'mapped_pagefile_regions_skipped', 'mapped_names_seen', 'mapped_name_query_failures'];
    const booleans = ['query_completed', 'mapped_file_query_used'];
    const fields = Object.hasOwn(value || {}, 'catalog') ? [...top, 'catalog'] : top;
    if (!keys(value, fields) || value.schema_version !== 1 || value.kind !== 'chatlens_windows_connection_diagnostics' || value.platform !== 'windows' || !code(value.connection_state) || !code(value.account_reason) || !code(value.collector_stage) || typeof value.collector_running !== 'boolean' || !Number.isSafeInteger(value.selected_conversation_count) || value.selected_conversation_count < 0 || value.selected_conversation_count > 50 || !keys(value.checks, checks) || checks.some(key => value.checks[key] !== null && typeof value.checks[key] !== 'boolean')) throw error('invalid_diagnostics');
    const detail = value.account_diagnostics;
    if (!object(detail) || Object.keys(detail).some(key => !['schema_version', ...counters, ...booleans].includes(key)) || Object.keys(detail).length > 0 && detail.schema_version !== 1 || booleans.some(key => Object.hasOwn(detail, key) && typeof detail[key] !== 'boolean') || counters.some(key => Object.hasOwn(detail, key) && (!Number.isSafeInteger(detail[key]) || detail[key] < 0 || detail[key] > 32768))) throw error('invalid_diagnostics');
    if (Object.hasOwn(value, 'catalog')) {
      const catalog = value.catalog, numbers = ['databases_checked', 'tables_seen', 'contacts_seen', 'conversations_found'];
      if (!keys(catalog, ['schema_version', 'state', 'phase', 'reason', 'elapsed_seconds', 'worker_active', ...numbers]) || catalog.schema_version !== 1 || !['idle', 'loading', 'ready', 'error'].includes(catalog.state) || !['idle', 'queued', 'validating_sources', 'opening_message', 'reading_message_tables', 'opening_contacts', 'reading_contacts', 'preparing_choices', 'complete', 'cancelled', 'failed'].includes(catalog.phase) || !code(catalog.reason) || typeof catalog.worker_active !== 'boolean' || count(catalog.elapsed_seconds) === null || catalog.elapsed_seconds > 86400 || numbers.some(key => count(catalog[key]) === null || catalog[key] > 1000000)) throw error('invalid_diagnostics');
    }
    return Object.fromEntries(fields.map(key => [key, value[key]]));
  }

  async function downloadDiagnostics() {
    if (!windows() || !state.connected || !state.generation || state.busy) return;
    const epoch = state.epoch, generation = state.generation;
    state.busy = 'diagnostics'; render();
    try {
      const result = connectionDiagnostics(await api('/api/setup/diagnostics'));
      if (epoch !== state.epoch || generation !== state.generation) return;
      const text = JSON.stringify(result, null, 2) + '\n';
      if (new TextEncoder().encode(text).length > 65536) throw error('invalid_diagnostics');
      const url = URL.createObjectURL(new Blob([text], { type: 'application/json;charset=utf-8' }));
      const link = node('a'); link.href = url; link.download = 'ChatLens-Windows-Connection-Diagnostics.json'; link.hidden = true;
      document.body.append(link);
      try { link.click(); } finally { link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
      feedback('连接诊断已导出到本机。');
    } catch (problem) {
      if (epoch === state.epoch && !ignored(problem)) {
        if (problem.status === 401) setError(problem);
        feedback(problem.code === 'invalid_diagnostics' ? '诊断格式暂不可用，请重新尝试。' : '诊断未能导出，请重新尝试。');
      }
    } finally { if (epoch === state.epoch) { state.busy = null; render(); schedule(state.epoch); } }
  }
  async function mutate(kind, profileId) {
    if (!state.connected || !state.generation || working()) return;
    if (['recheck', 'start'].includes(kind) && (!windows() || !connectionAction(kind))) return;
    if (['select', 'profile', 'catalog'].includes(kind) && windows() && connection()?.can_modify_scope !== true) return;
    if (kind === 'select' && (!canSelect() || !state.selectionSeeded || state.selected.size < 1 || state.selected.size > scopeLimit() || state.pickerGeneration !== state.generation || state.listLoading || state.listError)) return;
    if (kind === 'profile' && !state.setup?.profiles?.some(row => row.id === profileId && row.active !== true)) return;
    const epoch = state.epoch, generation = state.generation;
    if (kind === 'select' || kind === 'profile' || kind === 'start') state.enterWorkspace = true;
    if (kind === 'recheck') { state.enterWorkspace = false; state.stayOnSetup = true; }
    if (kind === 'catalog') {
      if (state.setup?.catalog?.worker_active === true) { feedback('正在结束上一次会话目录读取，请稍候再试。'); return; }
      state.editing = true; state.catalogError = null; state.listError = false; state.listRequest++; state.listLoading = false; state.loadedGeneration = null;
    }
    state.busy = kind; state.setupRequest++; render();
    try {
      const body = kind === 'select' ? { conversation_ids: [...state.selected.keys()], scope_generation: generation } : kind === 'profile' ? { profile_id: profileId, scope_generation: generation } : {};
      const result = await api('/api/setup/' + kind, { method: 'POST', body });
      if (epoch !== state.epoch) return;
      if (stages.has(result?.stage) && typeof result.account?.detected === 'boolean') acceptSetup(result); else acceptSetup(await api('/api/setup'));
    } catch (problem) {
      if (epoch === state.epoch && !ignored(problem)) {
        state.enterWorkspace = false;
        if (kind === 'catalog' && problem.status !== 401) { state.catalogError = problem.code || 'catalog_request_failed'; feedback('会话目录读取没有完成，可以在下方重试。'); }
        else setError(problem);
      }
    }
    finally { if (epoch === state.epoch) { state.busy = null; render(); if (canSelect() && state.loadedGeneration !== state.generation && !state.listLoading && !state.listError) loadConversations(); schedule(state.epoch); } }
  }
  function beginSelection() {
    if (!canManageSelection()) { feedback(windows() ? $('scope-manage-help').textContent || '请先完成上方账号与读取条件核验。' : '请等待本机读取条件就绪，再修改关注范围。'); return; }
    state.editing = true;
    render();
    $('selection-card').scrollIntoView({ block: 'start', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
    if (!catalogReady()) { mutate('catalog'); return; }
    if (state.loadedGeneration !== state.generation && !state.listLoading && !state.listError) loadConversations();
  }
  function retryConversations() {
    if (!canManageSelection()) { feedback('请等待当前步骤结束，并确认账号与读取条件已就绪。'); return; }
    if (!catalogReady()) mutate('catalog'); else loadConversations();
  }

  $('initialize-button').addEventListener('click', () => mutate('initialize')); $('retry-initialization').addEventListener('click', () => mutate('initialize'));
  $('start-selected').addEventListener('click', () => mutate('select')); $('manage-conversations').addEventListener('click', beginSelection); $('open-workspace').addEventListener('click', openWorkspace);
  $('windows-recheck').addEventListener('click', () => mutate('recheck')); $('windows-start').addEventListener('click', () => mutate('start'));
  $('download-connection-diagnostics').addEventListener('click', downloadDiagnostics);
  $('windows-offline').addEventListener('click', openWorkspace); $('windows-open-workspace').addEventListener('click', openWorkspace);
  $('retry-connection').addEventListener('click', connect); $('check-status').addEventListener('click', () => poll()); $('retry-conversations').addEventListener('click', retryConversations);
  for (const type of ['private', 'group']) $(type + '-list').addEventListener('keydown', event => { if (!['ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return; const rows = [...$(type + '-list').querySelectorAll('.picker-checkbox:not(:disabled)')]; if (!rows.length) return; const current = rows.indexOf(document.activeElement), next = event.key === 'Home' ? 0 : event.key === 'End' ? rows.length - 1 : (current + (['ArrowUp', 'ArrowLeft'].includes(event.key) ? -1 : 1) + rows.length) % rows.length; event.preventDefault(); rows[next].focus(); });
  $('setup-conversation-search').addEventListener('input', () => { clearTimeout(searchTimer); state.listRequest++; state.rows = []; state.listLoading = true; state.listError = false; renderSelection(); searchTimer = setTimeout(loadConversations, 250); });
  window.addEventListener('pagehide', () => { state.epoch++; state.setupRequest++; clearTimeout(state.timer); resetPicker(); state.setup = null; state.generation = null; state.connected = false; state.busy = null; state.enterWorkspace = false; render(); });
  window.addEventListener('pageshow', event => { if (event.persisted) { resetPicker(); state.setup = null; state.generation = null; state.stayOnSetup = true; state.redirecting = false; connect(); } });
  if (allowedOrigin()) connect(); else { setError(error('loopback_required')); $('setup-announcer').textContent = '请从本机启动器打开连接页面。'; }
})();
