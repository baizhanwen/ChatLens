/* ChatLens: file:// is fictional preview; loopback HTTP reads the authenticated local service. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const svgNS = 'http://www.w3.org/2000/svg';
  const initialDate = new Date();
  const LIVE = location.protocol !== 'file:';
  const live = { connected: false, ready: false, error: null, status: null, setup: null, session: null, scopeGeneration: null, scopeInvalid: false, timer: null, failures: 0, cycle: 0, selection: 0, overview: 0, search: null, searchRequest: 0, searching: false };
  const liveCache = new Map();
  const CACHE_LIMIT = 1000;
  let sequence = 100;
  let conversations = [];
  let activeId = 'lin';
  let typeFilter = 'all';
  let collectionState = 'running';
  let lastUpdate = new Date();
  let toastTimer;
  let currentView = 'overview';
  let previewMode = 'normal';
  let previewTimer;
  let wasMobile = window.innerWidth <= 640;
  let demoStartedAt = Date.now();
  let logRows = [];
  const preferences = { density: 'spacious', autoScroll: true };
  const pending = new Map();
  const scrollPositions = new Map();
  const ai = { initialized: false, status: null, provider: null, statusError: false, statusAt: 0, statusRequest: 0, threadId: null, turns: [], threads: [], threadRequest: 0, job: null, jobTimer: null, jobRequest: 0, cancelledRequest: false, cloudConsentAllowed: true, consentKey: null, scopeSignature: null, evidenceRequest: 0, testJob: null, testTimer: null, modelBusy: false, plan: null, planSignature: null, planRequest: 0, planTimer: null, planBusy: false, planSubmitting: false, planError: null, previewThreads: new Map() };

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function icon(name) {
    const svg = document.createElementNS(svgNS, 'svg');
    svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS(svgNS, 'use');
    use.setAttribute('href', '#i-' + name);
    svg.append(use);
    return svg;
  }

  function seedMessage(chat, index, sender, body, hour, minute, day = 0, direction = 'incoming') {
    const date = new Date(initialDate);
    date.setDate(date.getDate() + day);
    date.setHours(hour, minute, 0, 0);
    return { id: chat + '-' + index, sender, body, date, direction, own: direction === 'outgoing' };
  }

  function makeData() {
    const rows = [
      { id: 'lin', name: '林女士 · 澄禾商贸', short: '林', type: 'private', color: 'sage', subtitle: '私聊 · 虚构客户会话', unread: 0,
        messages: [
          ['林女士', '上午好，上一批样品已经收到了，门店反馈很不错。', 9, 12],
          ['我', '早上好！很高兴样品合适，新门店的数量确定了吗？', 9, 15, 0, 'outgoing'],
          ['林女士', '目前有两家店要一起上，想先订 800 套。', 9, 18],
          ['林女士', '另外包装上能加上我们自己的门店名称吗？', 9, 20],
          ['我', '可以的，包装文字确认后我们再安排打样。', 9, 23, 0, 'outgoing'],
          ['林女士', '好的，我今天把设计稿发给你们。', 9, 25],
          ['林女士', '数量确认了：浅灰色 500 套，米白色 300 套。\n麻烦按这个数量更新一下报价。', 10, 32],
          ['我', '收到，我按新的数量重新核算，稍后发你。', 10, 36, 0, 'outgoing'],
          ['林女士', '这批我们希望下周五之前到店，能帮忙确认一下排期吗？', 10, 42],
          ['林女士', '如果时间来得及，我们今天就可以走定金流程。', 10, 48]
        ] },
      { id: 'star', name: '星桥项目协作群', short: '星桥', type: 'group', color: 'sand', subtitle: '群聊 · 虚构项目沟通', unread: 2,
        messages: [
          ['许设计', '早，首页的第二版稿件已经整理好了。', 9, 10],
          ['陈经理', '我看了一下，新版留白舒服很多。', 9, 14],
          ['我', '下午我们把剩下两处文案过一遍。', 9, 19, 0, 'outgoing'],
          ['许设计', '可以，产品介绍的图也会一起补上。', 9, 30],
          ['陈经理', '客户希望周四能看到一个完整的预览版本。', 10, 8],
          ['我', '收到，先按周四安排，今天把反馈收齐。', 10, 12, 0, 'outgoing'],
          ['许设计', '还有一处想确认：品牌色继续沿用上一版的墨绿色吗？', 10, 37],
          ['陈经理', '对，品牌色保持一致，按钮可以再柔和一点。', 10, 51],
          ['许设计', '明白，更新好后我会在群里同步。', 10, 53]
        ] },
      { id: 'zhou', name: '周先生 · 远山工作室', short: '周', type: 'private', color: 'blue', subtitle: '私聊 · 虚构合作会话', unread: 1,
        messages: [
          ['周先生', '你好，想了解一下你们的长期合作方案。', 9, 5],
          ['我', '你好，我们可以先交流一下具体的使用场景。', 9, 9, 0, 'outgoing'],
          ['周先生', '主要是给我们工作室的客户做定制，每个月大约 200 到 300 份。', 9, 16],
          ['我', '了解。你们对交付周期有什么要求？', 9, 22, 0, 'outgoing'],
          ['周先生', '通常两周左右，偶尔会有比较急的项目。', 9, 27],
          ['我', '我整理一份按月合作的方案，今天晚些时候发你。', 9, 32, 0, 'outgoing'],
          ['周先生', '好的，辛苦。方案里也帮我列一下加急费用吧。', 10, 24]
        ] }
    ];
    return rows.map((chat) => ({ ...chat, messages: chat.messages.map((row, index) => seedMessage(chat.id, index, ...row)) }));
  }

  function activeChat() { return conversations.find((row) => row.id === activeId) || { id: null, name: '请选择会话', short: '聊', type: 'private', color: 'sage', subtitle: '仅显示所选会话', messages: [], unread: 0, count: 0 }; }
  function timeText(date) { return date instanceof Date && !Number.isNaN(date.getTime()) ? date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false }) : '—'; }
  function atBottom() { const scroll = $('message-scroll'); return scroll.scrollHeight - scroll.clientHeight - scroll.scrollTop < 75; }
  function announce(text) { $('announcer').textContent = text; }

  function toast(text) {
    clearTimeout(toastTimer);
    $('toast').textContent = text;
    $('toast').hidden = false;
    toastTimer = setTimeout(() => { $('toast').hidden = true; }, 3200);
  }

  function emptyState(title, description, symbol = 'search') {
    const node = element('div', 'empty-state');
    node.append(icon(symbol), element('strong', '', title), element('p', '', description));
    return node;
  }

  function renderConversations() {
    const query = $('conversation-search').value.trim().toLocaleLowerCase();
    const filtered = conversations.filter((chat) => (typeFilter === 'all' || chat.type === typeFilter) &&
      chat.name.toLocaleLowerCase().includes(query));
    const list = $('conversation-list');
    list.replaceChildren();
    for (const chat of filtered) {
      const last = LIVE ? { date: chat.latestDate, body: chat.latestPreview || '尚未采集到新文字', sender: '' } : chat.messages[chat.messages.length - 1];
      const button = element('button', 'conversation-item' + (chat.id === activeId ? ' selected' : ''));
      button.type = 'button';
      button.setAttribute('data-testid', 'conversation-item-' + chat.id);
      button.setAttribute('aria-label', chat.name + (chat.unread ? '，' + chat.unread + (LIVE ? ' 条本页未读消息' : ' 条演示未读消息') : ''));
      button.setAttribute('aria-pressed', String(chat.id === activeId));
      const avatar = element('span', 'avatar ' + chat.color, chat.short);
      avatar.setAttribute('aria-hidden', 'true');
      const copy = element('span', 'conversation-copy');
      const title = element('span', 'conversation-title-row');
      title.append(element('span', 'conversation-name', chat.name), element('time', 'conversation-time', timeText(last.date)));
      const preview = element('span', 'conversation-preview-row');
      const snippet = (chat.type === 'group' && last.sender ? last.sender + '：' : '') + last.body.replace(/\n/g, ' ');
      preview.append(element('span', 'conversation-preview', snippet));
      if (chat.unread) preview.append(element('span', 'unread-count', chat.unread > 99 ? '99+' : String(chat.unread)));
      copy.append(title, preview);
      button.append(avatar, copy);
      button.addEventListener('click', () => selectConversation(chat.id));
      list.append(button);
    }
    if (!filtered.length) list.append(emptyState(conversations.length ? '没有找到会话' : (LIVE ? '尚无可展示的会话' : '没有找到会话'), conversations.length ? '试试其他名称，或切换会话类型。' : '请确认本机采集服务已选择会话并完成初始化。'));
  }

  function appendHighlighted(node, text, query) {
    if (!query) { node.textContent = text; return; }
    const lowered = text.toLocaleLowerCase();
    const needle = query.toLocaleLowerCase();
    let cursor = 0;
    let start = lowered.indexOf(needle);
    while (start !== -1) {
      node.append(document.createTextNode(text.slice(cursor, start)));
      node.append(element('mark', '', text.slice(start, start + query.length)));
      cursor = start + query.length;
      start = lowered.indexOf(needle, cursor);
    }
    node.append(document.createTextNode(text.slice(cursor)));
  }

  function dayLabel(date) {
    if (!(date instanceof Date) || Number.isNaN(date.getTime())) return '来源时间未提供';
    const today = new Date();
    const sameDay = date.toDateString() === today.toDateString();
    return (sameDay ? '今天' : date.getFullYear() + '年') + ' · ' + (date.getMonth() + 1) + '月' + String(date.getDate()).padStart(2, '0') + '日';
  }

  function renderMessages(options = {}) {
    const scroll = $('message-scroll');
    const previousTop = scroll.scrollTop;
    const chat = activeChat();
    const query = $('message-search').value.trim();
    const lowered = query.toLocaleLowerCase();
    const searched = LIVE && query ? (live.search?.chatId === chat.id && live.search?.query === query ? live.search.messages : []) : null;
    const context = LIVE && !query && live.evidence?.chatId === chat.id ? live.evidence : null;
    const messages = context?.messages || searched || chat.messages.filter((message) => !query || (message.body + ' ' + message.sender).toLocaleLowerCase().includes(lowered));
    const list = $('message-list');
    list.replaceChildren();
    let previousDay = '';
    for (const message of messages) {
      const key = message.date ? message.date.toDateString() : 'unknown';
      if (key !== previousDay) {
        list.append(element('div', 'day-divider', dayLabel(message.date)));
        previousDay = key;
      }
      const direction = ['incoming', 'outgoing', 'unknown'].includes(message.direction) ? message.direction : 'unknown';
      const outgoing = direction === 'outgoing';
      const row = element('article', 'message-row message-' + direction + (outgoing ? ' own' : '') + (message.id === options.newId ? ' new-arrival' : '') + (context?.targetId === message.id ? ' is-evidence-target' : ''));
      row.setAttribute('data-message-id', message.id);
      row.dataset.direction = direction;
      row.setAttribute('aria-label', outgoing ? '本人发出的消息' : direction === 'incoming' ? '收到的消息' : '收发方向未确认的消息');
      const avatar = element('div', 'avatar ' + (outgoing ? 'own' : chat.color), outgoing ? '我' : message.sender.slice(0, 1));
      avatar.setAttribute('aria-hidden', 'true');
      const content = element('div', 'message-content');
      const meta = element('div', 'message-meta');
      const timestamp = element('time', '', timeText(message.date));
      if (message.date) timestamp.dateTime = message.date.toISOString();
      const directionLabel = element('span', 'message-direction-label', outgoing ? '本人' : direction === 'incoming' ? '收到' : '收发方向未确认');
      meta.append(element('span', 'message-sender', message.sender), directionLabel, timestamp);
      const body = element('p', 'message-body');
      appendHighlighted(body, message.body, query);
      content.append(meta, body);
      row.append(avatar, content);
      list.append(row);
    }
    $('message-count').textContent = context ? '原文附近 ' + messages.length + ' 条' : query ? (live.searching && LIVE ? '查找中…' : messages.length + ' 条结果') : (LIVE ? (chat.count || 0) : chat.messages.length) + ' 条';
    if (!messages.length) {
      if (LIVE && (live.searching || liveCache.get(chat.id)?.loading)) list.append(emptyState('正在读取消息', '正在从本机采集库读取已保存的文字。', 'refresh'));
      else list.append(emptyState(query ? '没有找到相关消息' : '等待新的文字消息', query ? (LIVE ? '换个关键词试试。搜索仅限当前所选会话。' : '换个关键词试试。搜索仅限当前虚构会话。') : '启用采集后收到的新文字会显示在这里。', query ? 'search' : 'messages'));
    }
    if (LIVE) updateOlderButton();
    if (options.bottom && !context) {
      scrollToBottom(false);
    } else if (options.restore !== undefined) {
      scroll.scrollTop = options.restore;
    } else {
      scroll.scrollTop = previousTop;
    }
  }

  function renderHeader() {
    const chat = activeChat();
    $('thread-avatar').className = 'thread-avatar ' + chat.color;
    $('thread-avatar').textContent = chat.short;
    $('thread-title').textContent = chat.name;
    $('thread-type').textContent = chat.type === 'group' ? '群聊' : '私聊';
    $('thread-subtitle').textContent = chat.subtitle + (LIVE ? ' · 本机已保存文字' : ' · 仅演示所选新文字');
  }

  function selectConversation(id) {
    if (LIVE) { selectLiveConversation(id); return; }
    $('citation-context').hidden = true;
    if ($('message-scroll').clientHeight > 0) scrollPositions.set(activeId, $('message-scroll').scrollTop);
    navigate('messages');
    const changed = activeId !== id;
    activeId = id;
    activeChat().unread = 0;
    renderStatus();
    if (changed) $('message-search').value = '';
    pending.set(id, 0);
    $('workbench').classList.add('mobile-thread-open');
    renderConversations();
    renderHeader();
    renderMessages({ bottom: !scrollPositions.has(id), restore: scrollPositions.get(id) });
    renderNewMessageButton();
    announce('已切换至' + activeChat().name + '，当前均为虚构消息。');
  }

  function renderNewMessageButton() {
    const count = pending.get(activeId) || 0;
    $('new-messages').hidden = !count;
    $('new-messages').querySelector('span').textContent = count + ' 条新消息，点击查看';
  }

  function scrollToBottom(smooth = true) {
    const scroll = $('message-scroll');
    smooth = smooth && !window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const previous = scroll.style.scrollBehavior;
    scroll.style.scrollBehavior = smooth ? 'smooth' : 'auto';
    scroll.scrollTo({ top: scroll.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
    scroll.style.scrollBehavior = previous;
    pending.set(activeId, 0);
    renderNewMessageButton();
  }

  const newTexts = {
    lin: ['刚和门店确认，包装文字没有其他修改了，可以按这版继续。', '补充一下：两家店分开收货，地址我稍后整理给你。', '我们这边已经确认好了，方便时帮我核对一下最终排期。'],
    star: ['今天的反馈已整理完，最后两处调整我会在下午更新。', '预览版本已经准备好，大家方便时可以一起确认。', '刚补充了移动端的细节，整体的字体间距也统一了。'],
    zhou: ['预算范围已经确认，麻烦把月度合作方案一起列出来。', '我们内部讨论了一下，希望下周安排一次沟通。', '定制数量可能会增加到每月 400 份，方案可以预留这个档位。']
  };

  function simulateMessage() {
    if (LIVE) return;
    if (collectionState !== 'running') {
      toast(collectionState === 'paused' ? '演示接收已暂停，恢复后再模拟新消息。' : '当前演示连接中断，请先演示重新连接。');
      return;
    }
    const chat = activeChat();
    const nearBottom = atBottom() && preferences.autoScroll;
    const body = newTexts[chat.id][sequence % newTexts[chat.id].length];
    const latestTime = chat.messages[chat.messages.length - 1].date.getTime();
    const message = { id: 'preview-new-' + sequence++, sender: chat.type === 'group' ? '许设计' : (chat.id === 'lin' ? '林女士' : '周先生'), body, date: new Date(Math.max(Date.now(), latestTime + 60000)), direction: 'incoming', own: false };
    chat.messages.push(message);
    lastUpdate = new Date();
    if (!nearBottom || $('message-search').value.trim()) pending.set(activeId, (pending.get(activeId) || 0) + 1);
    renderConversations();
    renderMessages({ bottom: nearBottom && !$('message-search').value.trim(), newId: message.id });
    renderNewMessageButton();
    renderStatus();
    addLog('received');
    announce('模拟收到一条新消息，来自' + chat.name + '。');
    if (nearBottom) toast('已添加一条虚构新消息。');
  }

  function setButtonIcon(button, name, text) {
    button.replaceChildren(icon(name), element('span', '', text));
  }

  function reset() {
    if (LIVE) return;
    conversations = makeData();
    activeId = 'lin';
    collectionState = 'running';
    sequence = 100;
    pending.clear();
    scrollPositions.clear();
    typeFilter = 'all';
    $('conversation-search').value = '';
    $('message-search').value = '';
    lastUpdate = new Date();
    demoStartedAt = Date.now();
    logRows = [];
    addLog('started');
    document.querySelectorAll('.filter-button').forEach((button) => {
      button.classList.toggle('active', button.dataset.filter === 'all');
      button.setAttribute('aria-pressed', String(button.dataset.filter === 'all'));
    });
    renderConversations();
    renderHeader();
    renderMessages({ bottom: true });
    renderStatus();
    renderNewMessageButton();
  }

  function renderStatus() {
    if (LIVE) { renderLiveStatus(); return; }
    if ($('collection-scope-count')) $('collection-scope-count').replaceChildren(document.createTextNode(String(conversations.length)), element('small', '', ' 个会话'));
    $('conversation-total').textContent = conversations.length;
    const states = {
      running: { title: '演示运行中', short: '运行中', stage: '演示接收中', color: 'running', description: '正在展示虚构消息的接收流程，没有连接真实采集服务。' },
      paused: { title: '演示已暂停', short: '已暂停', stage: '等待恢复演示', color: 'waiting', description: '虚构消息保留在本页，点击恢复后可以继续模拟来信。' },
      disconnected: { title: '演示连接中断', short: '连接中断', stage: '演示连接已中断', color: 'failure', description: '这里只演示连接中断，没有真实后台任务失败。重新连接后可继续体验。' }
    };
    const state = states[collectionState];
    const total = conversations.reduce((sum, chat) => sum + chat.messages.length, 0);
    $('ribbon-state').textContent = state.title;
    $('ribbon-description').textContent = state.short;
    $('ribbon-tag').className = 'tag tag-' + state.color;
    $('status-title').textContent = state.title;
    $('status-title').className = 'tag tag-' + state.color;
    $('status-description').textContent = state.description;
    $('fact-state').textContent = state.short;
    $('last-update').textContent = timeText(lastUpdate) + '（演示）';
    $('total-messages').textContent = total + ' 条';
    $('collection-total').replaceChildren(document.createTextNode(String(total)), element('small', '', ' 条'));
    $('collection-stage-title').textContent = state.stage;
    $('collection-stage-description').textContent = state.description;
    $('collection-stage-tag').className = 'tag tag-' + state.color;
    $('collection-stage-tag').textContent = state.title;
    $('nav-state-dot').style.background = 'var(--' + state.color + '-text)';
    $('nav-unread').textContent = String(conversations.reduce((sum, chat) => sum + chat.unread, 0));
    $('simulate-message').disabled = collectionState !== 'running';
    for (const id of ['toggle-collection', 'collection-toggle']) {
      $(id).disabled = collectionState === 'disconnected';
      setButtonIcon($(id), collectionState === 'paused' ? 'play' : 'pause', collectionState === 'paused' ? '恢复演示接收' : '暂停演示接收');
    }
    for (const id of ['connection-demo', 'collection-connection']) {
      setButtonIcon($(id), 'link', collectionState === 'disconnected' ? '演示重新连接' : '演示连接中断');
    }
    renderRecent();
  }

  function renderRecent() {
    if (ai.initialized) { renderAiScopes(); renderAiKnowledge(); }
  }

  function addLog(kind) {
    if (LIVE) return;
    const entries = {
      started: ['前端预览已就绪', '已载入 3 个虚构会话。真实采集服务尚未接入。'],
      received: ['新增一条虚构消息', '消息已添加至当前页面，未读取或保存真实聊天。'],
      paused: ['已暂停演示接收', '页面已有内容保留，可随时恢复演示。'],
      resumed: ['已恢复演示接收', '现在可以继续模拟新消息。'],
      disconnected: ['演示连接中断', '仅展示错误状态，不代表真实后台发生故障。'],
      reconnected: ['演示连接已恢复', '本地演示状态已恢复，真实服务仍未接入。']
    };
    logRows.unshift({ ...{ title: entries[kind][0], description: entries[kind][1] }, date: new Date() });
    logRows = logRows.slice(0, 8);
    const list = $('activity-log');
    list.replaceChildren();
    for (const row of logRows) {
      const item = element('li');
      const copy = element('div');
      copy.append(element('strong', '', row.title), element('p', '', row.description));
      item.append(element('span', 'status-dot'), copy, element('time', '', timeText(row.date)));
      list.append(item);
    }
    $('log-count').textContent = logRows.length + ' 条';
  }

  function navigate(view) {
    if (!['overview', 'messages', 'collection', 'settings'].includes(view)) return;
    const changed = currentView !== view;
    if (changed && currentView === 'settings') leaveSettings();
    if (changed && currentView === 'messages' && $('message-scroll').clientHeight > 0) {
      scrollPositions.set(activeId, $('message-scroll').scrollTop);
    }
    currentView = view;
    document.querySelectorAll('[data-view]').forEach(section => { section.hidden = section.dataset.view !== view; });
    document.querySelectorAll('[data-nav]').forEach(button => {
      const active = button.dataset.nav === view;
      button.classList.toggle('active', active);
      if (active) button.setAttribute('aria-current', 'page');
      else button.removeAttribute('aria-current');
    });
    $('breadcrumb-current').textContent = { overview: 'AI 助手', messages: '会话消息', collection: '采集状态', settings: '设置' }[view];
    if (changed) ai.evidenceRequest++;
    if (view === 'overview' && ai.initialized) refreshAiStatus();
    setDrawer(false, false);
    if (view === 'messages' && changed) {
      if (LIVE && activeChat().id && !live.evidence && !liveCache.get(activeId)?.loaded) ensureLiveMessages(activeId);
      renderMessages({ bottom: !scrollPositions.has(activeId), restore: scrollPositions.get(activeId) });
      renderNewMessageButton();
    }
    if (view === 'settings' && changed) {
      $('pref-density').value = preferences.density;
      $('pref-autoscroll').checked = preferences.autoScroll;
      loadModelSettings();
    }
    if (changed) window.scrollTo({ top: 0, behavior: 'instant' });
  }

  function setDetails(open) {
    const workbench = $('workbench');
    workbench.classList.toggle('details-open', open);
    workbench.classList.toggle('details-hidden', !open && window.innerWidth > 1250);
    $('toggle-details').setAttribute('aria-expanded', String(open));
    if (open && window.innerWidth <= 1250) requestAnimationFrame(() => $('status-panel').scrollIntoView({ block: 'nearest', behavior: 'instant' }));
  }

  function setDrawer(open, restoreFocus = true) {
    const mobile = window.innerWidth <= 640;
    open = open && mobile;
    document.body.classList.toggle('nav-open', open);
    $('sidebar-backdrop').hidden = !open;
    $('nav-toggle').setAttribute('aria-expanded', String(open));
    $('workspace').inert = open;
    $('sidebar').inert = mobile && !open;
    if (open) $('close-nav').focus();
    else if (restoreFocus && mobile) $('nav-toggle').focus();
  }

  function previewState(mode) {
    if (LIVE) return;
    clearTimeout(previewTimer);
    previewMode = mode;
    $('preview-state').value = mode;
    $('workbench').hidden = mode !== 'normal';
    $('view-state').hidden = mode === 'normal';
    $('view-state').className = 'card state-placeholder is-' + mode;
    const retry = $('retry-preview');
    retry.hidden = !['error', 'loading'].includes(mode);
    retry.disabled = mode === 'loading';
    retry.classList.toggle('is-loading', mode === 'loading');
    retry.setAttribute('aria-busy', String(mode === 'loading'));
    retry.textContent = mode === 'loading' ? '\u8f7d\u5165\u6f14\u793a\u4e2d' : '\u91cd\u65b0\u5c1d\u8bd5\u6f14\u793a';
    if (mode === 'normal') {
      renderMessages({ bottom: true });
      return;
    }
    const states = {
      loading: ['refresh', '正在载入虚构消息', '这是本地加载效果演示，当前没有后台请求或可计算进度。'],
      empty: ['messages', '还没有可展示的消息', '这是空白状态预览。你可以返回内容，继续查看虚构会话。'],
      error: ['info', '演示：暂时无法显示消息', '这是读取失败的视觉示例。点击重试仅会重新载入本页虚构数据。']
    };
    const row = states[mode];
    $('view-state-icon').replaceChildren(icon(row[0]));
    $('view-state-title').textContent = row[1];
    $('view-state-description').textContent = row[2];
    if (mode === 'loading') previewTimer = setTimeout(() => {
      if (previewMode === 'loading') previewState('normal');
    }, 800);
  }

  function toggleCollection() {
    if (LIVE) return;
    if (collectionState === 'disconnected') return;
    collectionState = collectionState === 'running' ? 'paused' : 'running';
    addLog(collectionState === 'paused' ? 'paused' : 'resumed');
    renderStatus();
    toast(collectionState === 'paused' ? '已暂停本页演示接收。' : '已恢复本页演示接收。');
  }

  function toggleConnection() {
    if (LIVE) return;
    collectionState = collectionState === 'disconnected' ? 'running' : 'disconnected';
    addLog(collectionState === 'disconnected' ? 'disconnected' : 'reconnected');
    renderStatus();
    toast(collectionState === 'disconnected' ? '正在演示连接中断，真实服务尚未接入。' : '演示连接已恢复。');
  }

  // The HTTP workspace reads committed local collection data. Its caches and
  // search cursors deliberately never share the fictional preview state.
  function liveDate(value) {
    if (!value) return null;
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function cacheFor(id) {
    if (!liveCache.has(id)) liveCache.set(id, { messages: [], loaded: false, loading: false, cursor: null, oldest: null, hasMore: false, request: 0, deferred: false });
    return liveCache.get(id);
  }

  function apiError(code, status = 0) {
    const error = new Error(code);
    error.code = code;
    error.status = status;
    return error;
  }

  function renderScopeControls() {
    const supported = LIVE && Boolean(live.scopeGeneration) && !live.scopeInvalid;
    $('scope-manage-sidebar').hidden = !supported;
    $('settings-scope-card').hidden = !supported;
    const connection = live.session?.platform === 'windows' ? live.setup?.connection : null;
    $('scope-settings-help').hidden = !supported || !connection || connection.can_modify_scope === true;
    $('scope-settings-help').textContent = connection?.state === 'explicit_read_only' ? '当前为离线只读，不能修改关注范围。你仍可进入连接引导，查看模式说明和已保存范围。' : connection?.state === 'cleanup_required' ? '上一轮采集尚未确认退出，暂不能修改范围。进入连接引导查看处理步骤。' : '需要先核验原账号的本机读取范围，才能修改会话。点击下方入口，在连接引导中重新检测并继续。';
  }

  function invalidateScope(navigateAway = true) {
    if (live.scopeInvalid) { if (navigateAway) location.replace('/connect.html?manage=1'); return; }
    live.scopeInvalid = true;
    live.connected = false;
    live.ready = false;
    live.scopeGeneration = null;
    live.session = null;
    live.status = null;
    live.setup = null;
    live.cycle++;
    live.selection++;
    live.searchRequest++;
    clearTimeout(live.timer);
    clearTimeout(live.searchTimer);
    live.search = null;
    live.evidence = null;
    liveCache.clear();
    conversations = [];
    activeId = null;
    pending.clear();
    scrollPositions.clear();
    logRows = [];
    ai.statusRequest++;
    ai.threadRequest++;
    ai.evidenceRequest++;
    ai.jobRequest++;
    ai.planRequest++;
    ai.modelFormRequest = (ai.modelFormRequest || 0) + 1;
    clearTimeout(ai.jobTimer);
    clearTimeout(ai.testTimer);
    clearTimeout(ai.planTimer);
    ai.job = ai.testJob = ai.plan = ai.provider = ai.status = null;
    ai.threadId = ai.planSignature = ai.consentKey = null;
    ai.cloudConsentAllowed = false;
    ai.turns = [];
    ai.threads = [];
    ai.previewThreads.clear();
    for (const id of ['ai-question', 'model-api-key', 'message-search', 'conversation-search']) $(id).value = '';
    $('ai-cloud-confirm').checked = false;
    for (const id of ['conversation-list', 'message-list', 'ai-turns', 'ai-thread-list', 'activity-log']) $(id).replaceChildren();
    $('ai-scope').replaceChildren(new Option('等待重新连接', 'all'));
    document.querySelectorAll('dialog[open]').forEach(dialog => dialog.close());
    document.querySelector('.app-shell').hidden = true;
    const notice = element('main', 'scope-change-card');
    notice.setAttribute('role', 'status');
    notice.append(element('h1', '', '关注范围已变化'), element('p', '', '正在重新接入当前范围，之前的消息与回答已从本页清除。'));
    document.body.append(notice);
    if (navigateAway) location.replace('/connect.html?manage=1');
  }

  async function api(route, token, options = {}) {
    if (live.scopeInvalid) throw apiError('scope_changed', 409);
    const generation = live.scopeGeneration;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const headers = token ? { Authorization: 'Bearer ' + token } : {};
      if (generation) headers['X-ChatLens-Scope'] = generation;
      if (options.method === 'POST') headers['Content-Type'] = 'application/json';
      const response = await fetch(new URL(route, location.origin), {
        credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
        headers, method: options.method || 'GET', body: options.method === 'POST' ? JSON.stringify(options.body || {}) : undefined
      });
      let result;
      try { result = await response.json(); } catch { throw apiError('invalid_response', response.status); }
      if (live.scopeInvalid || generation !== live.scopeGeneration) throw apiError('scope_changed', 409);
      if (!response.ok) {
        const code = /^[a-z0-9_]{1,100}$/.test(result?.error?.code || '') ? result.error.code : 'http_' + response.status;
        if (code === 'scope_changed') invalidateScope();
        throw apiError(code, response.status);
      }
      if (route !== '/api/session' && typeof result?.scope_generation === 'string' && result.scope_generation !== generation) { invalidateScope(); throw apiError('scope_changed', 409); }
      return result;
    } finally { clearTimeout(timeout); }
  }

  function messageRoute(id, params = {}) {
    const query = new URLSearchParams({ conversation: id, limit: '100' });
    for (const [name, value] of Object.entries(params)) if (value !== null && value !== undefined && value !== '') query.set(name, String(value));
    return '/api/messages?' + query;
  }

  function decodePage(payload, id) {
    if (!payload || !Array.isArray(payload.messages) || typeof payload.has_more !== 'boolean') throw apiError('invalid_response');
    const messages = payload.messages.map(row => {
      if (!row || !Number.isSafeInteger(row.id) || row.id < 1 || typeof row.body !== 'string' || typeof row.sender !== 'string' || (row.conversation_id || row.conversation) !== id) throw apiError('invalid_response');
      const date = typeof row.sent_at === 'number' ? liveDate(row.sent_at * 1000) : null;
      const direction = ['incoming', 'outgoing', 'unknown'].includes(row.direction) ? row.direction : 'unknown';
      return { id: row.id, sender: row.sender, body: row.body, date, observed: liveDate(row.observed_at), direction, own: direction === 'outgoing' };
    });
    const ids = messages.map(row => row.id);
    if (ids.some((value, index) => index && value <= ids[index - 1])) throw apiError('invalid_response');
    return { messages, hasMore: payload.has_more, before: ids[0] || null, after: ids.at(-1) || null };
  }

  function bindCache(id) {
    const chat = conversations.find(row => row.id === id);
    if (chat) chat.messages = cacheFor(id).messages;
  }

  function mergeMessages(cache, incoming) {
    const rows = new Map(cache.messages.map(row => [row.id, row]));
    for (const row of incoming) rows.set(row.id, row);
    cache.messages = [...rows.values()].sort((a, b) => a.id - b.id);
    cache.oldest = cache.messages[0]?.id || null;
  }

  function reconcileConversations(payload) {
    if (!payload || !Array.isArray(payload.conversations)) throw apiError('invalid_response');
    const previous = new Map(conversations.map(chat => [chat.id, chat]));
    const seen = new Set();
    const rows = payload.conversations.map(row => {
      if (!row || typeof row.id !== 'string' || !row.id || seen.has(row.id) || typeof row.name !== 'string' || !['private', 'group'].includes(row.type) || !Number.isSafeInteger(row.count) || row.count < 0) throw apiError('invalid_response');
      seen.add(row.id);
      const old = previous.get(row.id);
      const delta = old ? Math.max(0, row.count - old.count) : 0;
      const reading = row.id === activeId && currentView === 'messages' && !live.evidence && !$('message-search').value.trim() && atBottom() && preferences.autoScroll;
      if (delta && row.id === activeId && !reading) pending.set(row.id, (pending.get(row.id) || 0) + delta);
      return { id: row.id, name: row.name, type: row.type, short: row.name.slice(0, row.type === 'group' ? 2 : 1) || '聊', color: row.type === 'group' ? 'sand' : 'sage', subtitle: (row.type === 'group' ? '群聊' : '私聊') + ' · 仅所选会话', count: row.count, latestDate: liveDate(row.latest_at), latestPreview: typeof row.latest_preview === 'string' ? row.latest_preview : '', latestId: row.latest_id, messages: cacheFor(row.id).messages, unread: reading ? 0 : (old?.unread || 0) + delta };
    });
    conversations = rows;
    for (const id of liveCache.keys()) if (!seen.has(id)) { liveCache.delete(id); pending.delete(id); scrollPositions.delete(id); }
    if (!seen.has(activeId)) {
      activeId = rows[0]?.id || null;
      live.searchRequest++;
      live.search = null;
      live.searching = false;
      $('message-search').value = '';
      renderHeader();
      renderMessages();
    }
    renderConversations();
    renderHeader();
    renderRecent();
    renderNewMessageButton();
    const scope = document.querySelector('.sidebar-scope > span:nth-child(2)');
    scope.replaceChildren(icon('messages'), document.createTextNode(rows.filter(row => row.type === 'private').length + ' 个私聊 · ' + rows.filter(row => row.type === 'group').length + ' 个群聊'));
  }

  const liveStates = {
    starting: ['正在初始化', 'waiting', '采集程序正在初始化所选会话，请等待状态更新。'],
    collecting: ['正在采集', 'running', '本机采集程序在线。新文字保存后会自动出现在页面。'],
    stopping: ['正在停止采集', 'waiting', '采集程序正在保存进度并关闭。已保存的消息仍可查看。'],
    paused: ['采集已暂停', 'waiting', '采集程序报告已暂停。已保存的消息仍可查看。'],
    stopped: ['采集已停止', 'waiting', '采集程序已停止。重新启动本机采集程序后，页面会继续更新。'],
    error: ['采集异常', 'failure', '采集程序报告异常。请查看本机启动器的处理说明。'],
    stale: ['采集心跳已过期', 'waiting', '暂未收到新的采集心跳，页面保留已保存消息。请检查采集程序。'],
    unavailable: ['采集状态不可用', 'waiting', '尚未收到采集程序状态。已保存消息可查看，不能据此确认正在采集。']
  };

  function windowsConnectionDetails() {
    const value = live.session?.platform === 'windows' ? live.setup?.connection : null;
    if (!value) return null;
    if (value.state === 'account_unverified') return ['账号读取核验未完成', 'waiting', value.reason === 'windows_reader_configuration_unavailable' ? '原账号读取依据已确认，但读取配置未通过检查。请保持已登录状态，在连接引导中重新核验配置并查看固定诊断。' : value.checks?.process_running === true ? '微信程序已找到，但尚未定位账号读取依据，不代表未登录。请保持当前已登录状态，进入连接引导重新核验或反馈固定诊断。' : '微信程序的本机身份或访问条件尚未完成核验，无法据此判断是否登录。请进入连接引导查看固定诊断。'];
    const phase = typeof value.collector_stage === 'string' ? value.collector_stage : null;
    const running = typeof value.collector_running === 'boolean' ? value.collector_running : value.checks?.collecting === true;
    if (phase !== null && ['connected', 'stopped'].includes(value.state)) {
      if (running) {
        const stages = {
          starting: ['正在准备采集', 'waiting', '采集进程已经启动，正在等待本轮实际状态。原消息和进度保留，请勿重复开始。'],
          collecting: liveStates.collecting,
          paused: ['采集暂时暂停', 'waiting', '本轮采集进程仍在运行。若微信在前台或屏幕锁定，请让微信保持后台并解锁，再等待读取条件恢复。'],
          stale: ['等待本轮采集心跳', 'waiting', '暂未收到本轮新鲜心跳，不能据此认定采集已经停止。原内容保留，当前不会重复启动另一轮采集。'],
          error: ['本轮采集异常', 'failure', '本轮采集报告异常，进程仍在运行。请保留进度，并进入连接引导查看实际状态。'],
          stopping: ['正在停止采集', 'waiting', '正在保存本轮进度并结束进程，请等待正常退出，不要重复开始。'],
          stopped: ['正在确认采集退出', 'waiting', '采集已报告停止，正在等待本轮进程确认退出。原消息和检查点保留。']
        };
        return stages[phase] || stages.stale;
      }
      if (phase === 'error') return ['上一轮采集异常结束', 'failure', '原消息和检查点保留。请进入连接引导查看原因，重新检测后再开始采集。'];
    }
    if (value.checks?.collecting === true) return null;
    if (value.reason === 'windows_reader_configuration_unavailable') return ['读取配置需要检查', 'waiting', '原账号读取已核验，但现有配置尚未通过检查。进入连接引导查看原因，原数据和检查点会保留。'];
    const details = {
      waiting_for_wechat: ['等待本机微信', 'waiting', '请打开微信并由你完成登录，再进入连接引导点击“重新检测微信”。查看已有消息不表示正在采集。'],
      checking_account: ['正在核验账号读取', 'waiting', '正在检测微信程序和原账号的本机读取范围，请等待连接引导中的实际状态更新。'],
      wrong_account: ['微信账号不匹配', 'failure', '当前微信与这份资料的账号不一致，已停止采集并隔离原数据。请切回原账号后重新检测。'],
      connected: ['尚未开始采集', 'waiting', '原账号的本机读取范围已核验。在连接引导点击“开始采集”，即可从保存位置继续。'],
      stopped: ['采集已停止', 'waiting', '已保存消息仍可查看。在连接引导重新检测或点击“开始采集”继续，不必退出本工具。'],
      explicit_read_only: ['离线只读 · 未采集', 'waiting', '当前入口只查看已保存消息，不检测微信、不接收新消息，也不能修改关注范围。'],
      cleanup_required: ['等待上一轮采集退出', 'waiting', '上一轮采集尚未确认结束，暂不能开始或修改范围。请在连接引导查看处理步骤。']
    };
    return details[value.state] || ['等待连接微信', 'waiting', '请进入连接引导核对当前状态，再开始采集。'];
  }

  function renderWindowsConnectionBanner() {
    const show = live.session?.platform === 'windows' && !live.scopeInvalid;
    const banner = $('windows-workspace-connection');
    banner.hidden = !show;
    document.body.classList.toggle('windows-connection-visible', show);
    if (!show) return;
    const value = live.setup?.connection, details = windowsConnectionDetails();
    const running = typeof value?.collector_running === 'boolean' ? value.collector_running : value?.checks?.collecting === true;
    $('workspace-connection-title').textContent = details?.[0] || (running ? liveStates[live.status?.state]?.[0] || '采集进程已启动' : '连接微信与使用引导');
    $('workspace-connection-description').textContent = details?.[2] || (running ? (liveStates[live.status?.state]?.[2] || '采集进程运行中，实际阶段和新消息以本机保存结果为准。') : '在引导中检查微信程序、账号读取、配置和关注范围，再主动开始采集。');
    banner.classList.toggle('is-attention', Boolean(details) && details[1] !== 'failure');
    banner.classList.toggle('is-error', details?.[1] === 'failure');
    $('workspace-connection-guide').classList.toggle('button-primary', !running);
    renderScopeControls();
  }

  async function refreshWindowsConnection(epoch) {
    if (live.session?.platform !== 'windows') return true;
    const setup = await api('/api/setup');
    if (epoch !== live.cycle) return false;
    if (!setup || setup.platform !== 'windows' || setup.connection && (typeof setup.connection.can_read_saved !== 'boolean' || typeof setup.connection.can_modify_scope !== 'boolean')) throw apiError('invalid_response');
    live.setup = setup;
    renderWindowsConnectionBanner();
    if (setup.connection?.can_read_saved === false) {
      if (conversations.length || ai.turns.length || liveCache.size) { invalidateScope(); return false; }
      live.connected = false;
      live.ready = true;
      live.error = null;
      live.failures = 0;
      live.status = null;
      renderLiveStatus();
      renderAiControls();
      return false;
    }
    return true;
  }

  function stateDetails() {
    if (live.error) return [live.error.status === 401 ? '本机会话已失效' : '页面连接中断', 'failure', live.error.status === 401 ? '请从本机启动器重新打开工作台。已显示内容保留，暂不更新。' : '暂时无法连接本机服务。已显示内容可能已过期，恢复连接后会继续读取。'];
    const connectionDetails = windowsConnectionDetails();
    if (connectionDetails) return connectionDetails;
    if (!live.connected) return ['正在连接本机服务', 'waiting', '正在读取真实采集状态和已保存消息。'];
    return liveStates[live.status?.state] || liveStates.unavailable;
  }

  function updateLiveElapsed() {
    const started = liveDate(live.status?.started_at);
    const end = ['stopped', 'error', 'paused', 'stale', 'unavailable'].includes(live.status?.state) ? liveDate(live.status?.heartbeat_at) : new Date();
    if (!started || !end) { $('demo-elapsed').textContent = '—'; return; }
    const seconds = Math.max(0, Math.floor((end - started) / 1000));
    $('demo-elapsed').textContent = (seconds >= 3600 ? String(Math.floor(seconds / 3600)) + ':' : '') + String(Math.floor(seconds / 60) % (seconds >= 3600 ? 60 : 100000)).padStart(2, '0') + ':' + String(seconds % 60).padStart(2, '0');
  }

  function renderLiveStatus() {
    const [title, color, description] = stateDetails();
    const total = live.status?.total_messages ?? conversations.reduce((sum, chat) => sum + chat.count, 0);
    const saved = liveDate(live.status?.last_update);
    $('ribbon-state').textContent = title;
    $('ribbon-description').textContent = live.connected ? '本机采集 · 仅所选会话' : '已显示内容暂不更新';
    $('ribbon-tag').className = 'tag tag-' + color;
    for (const id of ['status-title', 'collection-stage-tag']) { $(id).textContent = title; $(id).className = 'tag tag-' + color; }
    for (const id of ['status-description', 'collection-stage-description']) $(id).textContent = description;
    $('collection-stage-title').textContent = title;
    $('fact-state').textContent = title;
    $('last-update').textContent = saved ? saved.toLocaleString('zh-CN', { hour12: false }) : '尚无保存记录';
    $('total-messages').textContent = total + ' 条';
    $('collection-total').replaceChildren(document.createTextNode(String(total)), element('small', '', ' 条'));
    $('collection-scope-count').replaceChildren(document.createTextNode(String(conversations.length)), element('small', '', ' 个会话'));
    $('conversation-total').textContent = conversations.length;
    $('nav-state-dot').style.background = 'var(--' + color + '-text)';
    $('nav-unread').textContent = String(conversations.reduce((sum, chat) => sum + chat.unread, 0));
    $('live-connection-notice').hidden = !live.error;
    $('live-connection-description').textContent = description;
    $('live-retry').disabled = live.error?.status === 401;
    $('live-retry').textContent = live.error?.status === 401 ? '请从启动器重新打开' : '重新连接';
    document.querySelector('.sidebar-local > div').replaceChildren(document.createTextNode('本机采集'), element('span', '', live.error ? '连接已中断 · 内容可能已过期' : live.connected ? title : '正在连接本机服务'));
    $('live-page-state').textContent = live.connected || live.setup && !live.error ? '本机服务已连接' : '尚未连接';
    $('live-resume-state').textContent = live.status?.resumed_from_checkpoint === true ? '已从保存位置继续' : live.status?.resumed_from_checkpoint === false ? '本次首次启用' : '暂无恢复记录';
    const heartbeat = liveDate(live.status?.heartbeat_at);
    $('live-heartbeat').textContent = heartbeat ? heartbeat.toLocaleTimeString('zh-CN', { hour12: false }) : '尚未收到';
    document.querySelectorAll('.property-list dd')[2].textContent = title;
    renderWindowsConnectionBanner();
    updateLiveElapsed();
  }

  function recordLiveStatus(status) {
    const previous = live.status;
    live.status = status;
    if (previous?.state === status.state) return;
    const [title, , description] = liveStates[status.state] || liveStates.unavailable;
    logRows.unshift({ title, description, date: liveDate(status.updated_at) });
    logRows = logRows.slice(0, 8);
    const list = $('activity-log');
    list.replaceChildren();
    for (const row of logRows) {
      const item = element('li');
      const copy = element('div');
      copy.append(element('strong', '', row.title), element('p', '', row.description));
      item.append(element('span', 'status-dot'), copy, element('time', '', timeText(row.date)));
      list.append(item);
    }
    $('log-count').textContent = logRows.length + ' 条';
  }

  function acceptStatus(status) {
    if (!status || !Object.hasOwn(liveStates, status.state) || !Number.isSafeInteger(status.total_messages) || status.total_messages < 0) throw apiError('invalid_response');
    recordLiveStatus(status);
  }

  function showLiveFailure(error) {
    live.connected = false;
    live.error = { status: error?.status || 0 };
    live.failures++;
    renderLiveStatus();
  }

  function scheduleLive(epoch) {
    clearTimeout(live.timer);
    if (live.scopeInvalid || live.error?.status === 401 || epoch !== live.cycle) return;
    live.timer = setTimeout(() => live.ready ? pollLive(epoch) : connectLive(), live.failures ? Math.min(10000, 1000 * 2 ** Math.min(live.failures, 4)) : 1000);
  }

  async function connectLive() {
    if (live.scopeInvalid) return;
    const epoch = ++live.cycle;
    clearTimeout(live.timer);
    live.error = null;
    live.connected = false;
    renderLiveStatus();
    try {
      if (location.protocol !== 'http:' || location.hostname !== '127.0.0.1') throw apiError('loopback_required', 403);
      let token = new URLSearchParams(location.hash.slice(1)).get('token');
      const session = await api('/api/session', token);
      token = null;
      if (epoch !== live.cycle) return;
      if (session?.mode !== 'live' || session.capabilities?.read_messages !== true) throw apiError('invalid_response');
      const generation = typeof session.scope_generation === 'string' && /^[a-zA-Z0-9_-]{16,128}$/.test(session.scope_generation) ? session.scope_generation : null;
      if (live.session && generation !== live.scopeGeneration || session.scope_switching === true) { invalidateScope(); return; }
      if (session.scope_generation !== undefined && !generation) throw apiError('invalid_response');
      live.scopeGeneration = generation;
      live.session = session;
      renderScopeControls();
      if (location.hash) history.replaceState(null, '', location.pathname + location.search);
      if (!await refreshWindowsConnection(epoch)) return;
      const [metadata, status] = await Promise.all([api('/api/conversations'), api('/api/status')]);
      if (epoch !== live.cycle) return;
      acceptStatus(status);
      reconcileConversations(metadata);
      live.ready = true;
      live.connected = true;
      live.failures = 0;
      renderLiveStatus();
      refreshAiStatus(true);
      refreshAiThreads();
      if (activeId) await ensureLiveMessages(activeId);
    } catch (error) { if (epoch === live.cycle) showLiveFailure(error); }
    finally { scheduleLive(epoch); }
  }

  async function pollLive(epoch) {
    try {
      if (!await refreshWindowsConnection(epoch)) return;
      const [metadata, status] = await Promise.all([api('/api/conversations'), api('/api/status')]);
      if (epoch !== live.cycle) return;
      acceptStatus(status);
      reconcileConversations(metadata);
      live.connected = true;
      live.error = null;
      live.failures = 0;
      renderLiveStatus();
      refreshAiStatus();
      if (activeId) await appendLiveMessages(activeId);
    } catch (error) { if (epoch === live.cycle) showLiveFailure(error); }
    finally { scheduleLive(epoch); }
  }

  async function ensureLiveMessages(id, latest = false) {
    const cache = cacheFor(id);
    const epoch = live.cycle;
    if (cache.loaded && !latest) return;
    if (cache.loading && !latest) return;
    const request = ++cache.request;
    cache.loading = true;
    if (id === activeId) renderMessages();
    try {
      const page = decodePage(await api(messageRoute(id)), id);
      if (request !== cache.request || !liveCache.has(id)) return;
      cache.messages = page.messages;
      cache.loaded = true;
      cache.hasMore = page.hasMore;
      cache.cursor = page.after;
      cache.oldest = page.before;
      cache.deferred = false;
      bindCache(id);
      if (id === activeId && !$('message-search').value.trim()) {
        renderMessages({ bottom: latest || !scrollPositions.has(id), restore: latest ? undefined : scrollPositions.get(id) });
        if (latest) { pending.set(id, 0); activeChat().unread = 0; }
      }
      renderConversations();
      renderNewMessageButton();
      if (latest) renderLiveStatus();
    } catch (error) { if (request === cache.request && epoch === live.cycle) showLiveFailure(error); }
    finally { if (request === cache.request) { cache.loading = false; if (id === activeId) { renderMessages(); updateOlderButton(); } } }
  }

  async function appendLiveMessages(id) {
    const cache = cacheFor(id);
    if (!cache.loaded) { if (!live.evidence) await ensureLiveMessages(id); return; }
    if (cache.loading || cache.deferred) return;
    const request = ++cache.request;
    const canFollow = () => id === activeId && currentView === 'messages' && !live.evidence && atBottom() && preferences.autoScroll && !$('message-search').value.trim();
    const wasFollowing = canFollow();
    const previousCursor = cache.cursor || 0;
    cache.loading = true;
    try {
      // Drain bounded pages; the next poll resumes from the committed rowid.
      for (let index = 0; index < 5; index++) {
        const page = decodePage(await api(messageRoute(id, { after: cache.cursor || 0 })), id);
        if (request !== cache.request || !liveCache.has(id)) return;
        if (page.messages.length) {
          if (cache.cursor && page.after <= cache.cursor) throw apiError('invalid_response');
          mergeMessages(cache, page.messages);
          cache.cursor = page.after;
          if (cache.messages.length > CACHE_LIMIT) {
            if (canFollow() || id !== activeId) {
              cache.messages = cache.messages.slice(-CACHE_LIMIT);
              cache.oldest = cache.messages[0].id;
              cache.hasMore = true;
            } else cache.deferred = true;
          }
          bindCache(id);
        }
        if (!page.hasMore || cache.deferred) break;
      }
      if (id === activeId) {
        const follow = canFollow();
        if (wasFollowing && !follow) pending.set(id, Math.max(pending.get(id) || 0, cache.messages.filter(row => row.id > previousCursor).length));
        if (!$('message-search').value.trim() && !live.evidence) renderMessages({ bottom: follow });
        renderNewMessageButton();
      }
      renderConversations();
    } catch (error) { if (request === cache.request) throw error; }
    finally { if (request === cache.request) cache.loading = false; }
  }

  async function selectLiveConversation(id, options = {}) {
    if (!conversations.some(row => row.id === id)) return;
    live.evidence = options.context || null;
    $('citation-context').hidden = !options.context;
    if ($('message-scroll').clientHeight > 0) scrollPositions.set(activeId, $('message-scroll').scrollTop);
    const changed = id !== activeId;
    activeId = id;
    activeChat().unread = 0;
    if (changed) {
      $('message-search').value = '';
      live.searchRequest++;
      live.search = null;
      live.searching = false;
    }
    pending.set(id, 0);
    navigate('messages');
    $('workbench').classList.add('mobile-thread-open');
    renderConversations();
    renderHeader();
    renderMessages({ bottom: !scrollPositions.has(id), restore: scrollPositions.get(id) });
    renderNewMessageButton();
    renderLiveStatus();
    if (!options.context) await ensureLiveMessages(id);
    announce('已切换至' + (conversations.find(row => row.id === id)?.name || '所选会话') + '。');
  }

  let searchDelay;
  function requestLiveSearch() {
    clearTimeout(searchDelay);
    const query = $('message-search').value.trim();
    const id = activeId;
    const request = ++live.searchRequest;
    live.search = null;
    live.searching = Boolean(query && id);
    renderMessages({ restore: 0 });
    if (!query || !id) return;
    searchDelay = setTimeout(async () => {
      try {
        const page = decodePage(await api(messageRoute(id, { q: query })), id);
        if (request !== live.searchRequest || id !== activeId || query !== $('message-search').value.trim()) return;
        live.search = { chatId: id, query, messages: page.messages, hasMore: page.hasMore, before: page.before };
      } catch (error) { if (request === live.searchRequest) showLiveFailure(error); }
      finally { if (request === live.searchRequest && id === activeId) { live.searching = false; renderMessages({ restore: 0 }); } }
    }, 200);
  }

  function updateOlderButton() {
    const query = $('message-search').value.trim();
    const cache = cacheFor(activeId);
    if (live.evidence?.chatId === activeId) { $('load-older').hidden = true; return; }
    $('load-older').hidden = query ? !live.search?.hasMore : !cache.hasMore;
    $('load-older').disabled = cache.loading || live.searching;
    $('load-older').textContent = cache.loading || live.searching ? '正在读取…' : query ? '加载更早的匹配消息' : '加载更早消息';
  }

  async function loadOlder() {
    if (!LIVE || !activeId) return;
    const id = activeId;
    const query = $('message-search').value.trim();
    const cache = cacheFor(id);
    const search = live.search;
    if (cache.loading || live.searching || (query ? !search?.hasMore : !cache.hasMore)) return;
    const request = query ? live.searchRequest : ++cache.request;
    const scroll = $('message-scroll');
    const previousHeight = scroll.scrollHeight;
    const previousTop = scroll.scrollTop;
    if (query) live.searching = true;
    else cache.loading = true;
    updateOlderButton();
    try {
      const page = decodePage(await api(messageRoute(id, { before: query ? search.before : cache.oldest, q: query })), id);
      if (query) {
        if (request !== live.searchRequest || live.search !== search) return;
        const byId = new Map([...page.messages, ...search.messages].map(row => [row.id, row]));
        search.messages = [...byId.values()].sort((a, b) => a.id - b.id);
        search.before = search.messages[0]?.id || search.before;
        search.hasMore = page.hasMore;
      } else {
        if (request !== cache.request) return;
        mergeMessages(cache, page.messages);
        cache.hasMore = page.hasMore;
        bindCache(id);
      }
      if (id === activeId && query === $('message-search').value.trim()) {
        renderMessages();
        scroll.scrollTop = previousTop + scroll.scrollHeight - previousHeight;
      }
    } catch (error) { showLiveFailure(error); }
    finally {
      if (query && request === live.searchRequest) live.searching = false;
      else if (!query && request === cache.request) cache.loading = false;
      if (id === activeId) { renderMessages(); updateOlderButton(); }
    }
  }

  async function jumpLiveLatest() {
    const id = activeId;
    live.evidence = null;
    $('citation-context').hidden = true;
    $('message-search').value = '';
    live.searchRequest++;
    live.search = null;
    live.searching = false;
    const button = $('live-refresh');
    if (button?.disabled) return;
    if (button) { button.disabled = true; button.classList.add('is-loading'); button.setAttribute('aria-busy', 'true'); }
    try {
      if (id) await ensureLiveMessages(id, true);
      if (id === activeId && !$('message-search').value.trim()) scrollToBottom();
    }
    finally { if (button) { button.disabled = false; button.classList.remove('is-loading'); button.setAttribute('aria-busy', 'false'); } }
  }

  function configureLiveUI() {
    document.body.classList.add('live-mode');
    document.querySelectorAll('[data-preview-only]').forEach(node => { node.hidden = true; });
    ['simulate-message', 'toggle-collection', 'connection-demo', 'collection-toggle', 'collection-connection'].forEach(id => { $(id).hidden = true; });
    document.querySelector('.preview-label').replaceChildren(element('span', 'status-dot'), document.createTextNode('本机采集 · 所选会话'));
    document.querySelector('.user-avatar').title = '本机工作台';
    document.querySelector('.user-avatar').setAttribute('aria-label', '本机工作台');
    document.querySelector('.sidebar-scope > p').replaceChildren(document.createTextNode('只关注你选择的沟通。'), element('br'), document.createTextNode('采集的文字仅保存在本机。'));
    document.querySelector('.sidebar-scope > span:nth-child(2)').replaceChildren(icon('messages'), document.createTextNode('正在读取所选会话'));
    document.querySelector('.sidebar-version > span:last-child').textContent = 'v0.3 · LOCAL WORKSPACE';
    $('preview-help').querySelector('span').textContent = '使用说明';
    document.querySelector('#view-messages .page-heading > .tag').textContent = '本机真实数据';
    document.querySelector('.conversation-note').replaceChildren(icon('shield'), document.createTextNode('仅所选会话 · 启用后的新文字'));
    document.querySelector('.ribbon-preview').textContent = '真实采集状态';
    document.querySelector('.message-footer > span').textContent = '只读查看 · 不会代发消息';
    const refresh = element('button', 'button', '刷新消息');
    refresh.id = 'live-refresh';
    refresh.addEventListener('click', jumpLiveLatest);
    document.querySelector('.message-footer').append(refresh);
    $('status-panel').setAttribute('aria-label', '真实采集状态');
    const labels = document.querySelectorAll('.status-facts dt');
    ['已保存消息', '最近保存', '采集状态', '页面连接'].forEach((text, index) => { labels[index].textContent = text; });
    document.querySelector('.status-facts > div:last-child dd').id = 'live-page-state';
    for (const [label, id] of [['恢复方式', 'live-resume-state'], ['最近心跳', 'live-heartbeat']]) {
      const row = element('div');
      const value = element('dd', '', '正在读取');
      value.id = id;
      row.append(element('dt', '', label), value);
      document.querySelector('.status-facts').append(row);
    }
    document.querySelector('.panel-note > p').textContent = '采集由本机启动器管理。页面只读取已保存消息，不控制微信。';
    document.querySelector('#view-collection .page-heading p').textContent = '查看真实采集阶段、已保存消息和状态变化。';
    document.querySelector('#view-collection .page-heading > .tag').textContent = '本机采集服务';
    document.querySelectorAll('.stage-metrics > div > span')[0].textContent = '本次运行时间';
    document.querySelectorAll('.stage-metrics > div > span')[1].textContent = '已保存消息';
    const statusRefresh = element('button', 'button button-primary', '重新检查状态');
    statusRefresh.id = 'live-status-refresh';
    statusRefresh.addEventListener('click', connectLive);
    document.querySelector('.stage-actions').append(statusRefresh);
    document.querySelector('.log-card h2').textContent = '页面观察到的状态变化';
    $('log-count').textContent = '0 条';
    $('activity-log').append(emptyState('正在读取状态', '状态变化会记录在本页，刷新后还原。', 'pulse'));
    document.querySelectorAll('.property-list dd').forEach((node, index) => { node.textContent = ['本机采集库', '每秒检查已保存消息', '正在连接', '未启用'][index]; });
    document.querySelector('.technical-details summary').textContent = '本机读取说明';
    document.querySelector('.technical-details p').textContent = '采集只写入本机已选会话的新文字。主动向 AI 提问时，只检索提问范围内的相关片段，并使用你确认的目标模型。';
    $('preview-dialog-title').textContent = '关注你选择的沟通。';
    const help = document.querySelectorAll('#preview-dialog > p');
    help[0].textContent = '这是本机采集工作台，只展示采集程序中已经选择的会话及启用后保存的文字消息。';
    help[1].textContent = '请保持电脑、微信与采集程序运行。AI 模型需要你在本机配置，只在主动提问或测试时连接。采集异常、停止或心跳过期会明确显示，不会代发微信消息。';
    $('confirm-preview').textContent = '知道了';
    ['message-search'].forEach(id => { $(id).maxLength = 256; });
    $('live-retry').addEventListener('click', connectLive);
    if (typeof ResizeObserver === 'function') new ResizeObserver(entries => { for (const entry of entries) document.body.style.setProperty('--workspace-connection-height', entry.target.getBoundingClientRect().height + 'px'); }).observe($('windows-workspace-connection'));
    $('load-older').addEventListener('click', loadOlder);
  }

  function initializeLive() {
    activeId = null;
    configureLiveUI();
    renderConversations();
    renderHeader();
    renderMessages();
    renderRecent();
    connectLive();
  }

  function aiPost(route, body = {}) { return api(route, null, { method: 'POST', body }); }
  function aiBusy() { return Boolean(ai.job || ai.testJob || ai.modelBusy || ai.threadLoading || ai.planSubmitting); }
  function aiScope() { const selected = $('ai-scope').value; return selected === 'all' ? conversations.map(row => row.id) : conversations.filter(row => row.id === selected).map(row => row.id); }
  function aiPlanInput() { return { question: $('ai-question').value.trim(), scope: aiScope(), time_range: $('ai-time-range').value, ...(ai.threadId ? { thread_id: ai.threadId } : {}) }; }
  function aiPlanSignature() { return JSON.stringify(aiPlanInput()); }
  function currentAiPlan() { return ai.planSignature === aiPlanSignature() ? ai.plan : null; }
  function aiScopeKey() {
    const plan = currentAiPlan();
    return JSON.stringify([ai.provider?.target_fingerprint || '', aiScope().sort(), $('ai-time-range').value, $('ai-question').value.trim().replace(/\s+/g, ' '), plan?.route || '', plan?.time_label || '', plan?.time_range?.start || '', plan?.scope || []]);
  }
  function cleanAiPlan(value) {
    if (!value || !['local_stats', 'analysis', 'mixed', 'clarify'].includes(value.route) || typeof value.requires_model !== 'boolean' || !Array.isArray(value.scope) || !value.scope.every(id => typeof id === 'string') || typeof value.time_label !== 'string') return null;
    if (value.requires_model !== ['analysis', 'mixed'].includes(value.route)) return null;
    return { route: value.route, requires_model: value.requires_model, metric: typeof value.metric === 'string' ? value.metric : null, time_label: value.time_label, time_range: value.time_range && typeof value.time_range === 'object' ? value.time_range : {}, scope: [...value.scope], scope_label: typeof value.scope_label === 'string' ? value.scope_label : '', summary: typeof value.summary === 'string' ? value.summary : '', clarification: typeof value.clarification === 'string' ? value.clarification : null, warnings: Array.isArray(value.warnings) ? value.warnings.filter(row => typeof row === 'string') : [], source: 'local' };
  }
  function aiRouteLabel(plan) { return { local_stats: '本机统计', analysis: '模型分析', mixed: '统计与分析', clarify: '需要确认' }[plan?.route] || '核对提问范围'; }
  function aiPlanScopeLabel(plan) {
    if (plan.scope_label) return plan.scope_label;
    const names = plan.scope.map(id => conversations.find(chat => chat.id === id)?.name).filter(Boolean);
    return names.length === 1 ? names[0] : names.length ? names.length + ' 个关注会话' : '未匹配到会话';
  }
  function aiPlanDescription(plan) {
    const summary = plan.summary || '';
    return [...new Set([plan.time_label, aiPlanScopeLabel(plan)].filter(label => label && !summary.includes(label)).concat(summary).filter(Boolean))].join(' · ');
  }
  function renderAiPlan() {
    const plan = currentAiPlan();
    const visible = LIVE && Boolean($('ai-question').value.trim()) && (ai.planBusy || ai.planError || plan);
    $('ai-plan-preview').hidden = !visible;
    $('ai-plan-preview').classList.toggle('is-error', Boolean(ai.planError));
    $('ai-plan-route').textContent = ai.planBusy ? '正在核对范围' : ai.planError ? '范围暂不可用' : aiRouteLabel(plan);
    $('ai-plan-description').textContent = ai.planBusy ? '仅在本机查询，不会调用模型。' : ai.planError || (plan ? aiPlanDescription(plan) : '');
    const explanation = [plan?.clarification, ...(plan?.warnings || [])].filter(Boolean).join(' ');
    $('ai-plan-clarification').textContent = explanation;
    $('ai-plan-clarification').hidden = !explanation;
  }
  function queueAiPlan() {
    clearTimeout(ai.planTimer);
    ai.consentKey = null;
    ai.planRequest++;
    ai.plan = null;
    ai.planSignature = null;
    ai.planBusy = false;
    ai.planError = null;
    renderAiControls();
    if (!LIVE || !$('ai-question').value.trim() || !live.connected || !aiScope().length) return;
    ai.planTimer = setTimeout(() => requestAiPlan().catch(() => {}), 350);
  }
  async function requestAiPlan() {
    clearTimeout(ai.planTimer);
    const payload = aiPlanInput();
    const signature = JSON.stringify(payload);
    const request = ++ai.planRequest;
    ai.planBusy = true;
    ai.planError = null;
    renderAiControls();
    try {
      const result = await aiPost('/api/ai/plan', payload);
      if (request !== ai.planRequest || signature !== aiPlanSignature()) return null;
      const plan = cleanAiPlan(result?.plan);
      if (!plan || plan.scope.some(id => !payload.scope.includes(id))) throw apiError('invalid_response');
      ai.plan = plan;
      ai.planSignature = signature;
      return plan;
    } catch (problem) {
      if (request === ai.planRequest) { ai.plan = null; ai.planSignature = null; ai.planError = '暂时无法核对查询范围，请重试发送。'; }
      throw problem;
    } finally {
      if (request === ai.planRequest) { ai.planBusy = false; renderAiControls(); }
    }
  }
  function aiTarget(provider = ai.provider) {
    if (!provider?.base_url || !provider?.model) return '未配置模型';
    try { return new URL(provider.base_url).host + ' · ' + provider.model; } catch { return '模型地址需要检查'; }
  }
  function aiNearBottom() { const scroll = $('ai-chat-scroll'); return scroll.scrollHeight - scroll.clientHeight - scroll.scrollTop < 85; }
  function aiNotice(text = '') { $('ai-form-notice').hidden = !text; $('ai-form-notice').textContent = text; }
  function aiErrorText(problem) {
    const code = problem?.code || '';
    if (code === 'ai_history_limit') return '当前对话记录已较长，本轮未调用模型，请开始新对话继续。';
    if (code === 'ai_answer_too_large') return '本轮回答过长或格式异常，请缩小问题范围后重试。';
    if (code === 'ai_provider_output_limit') return '模型输出达到长度上限，请缩小问题范围后重试。';
    if (problem?.status === 401) return '本机会话已失效，请从启动器重新打开工作台。';
    if (code === 'ai_provider_url_invalid') return '请填写 HTTPS 模型 API 地址，或本机回环地址。';
    if (code === 'ai_provider_model_invalid') return '请填写有效的模型名称。';
    if (code === 'ai_question_invalid') return '请输入不超过 2000 字的问题。';
    if (/target|fingerprint|confirm/.test(code)) return '模型目标或提问范围已变化，请核对下方目标并重新确认。';
    if (/key|auth|credential/.test(code)) return '模型认证没有通过，请在本机检查 API Key 和服务地址。';
    if (/citation/.test(code)) return '模型回答的引用没有通过核对，本轮没有展示未核实的回答。可以重新提问。';
    if (/empty|evidence|knowledge|no_context/.test(code)) return '所选范围内暂时没有足够的原消息证据。试试调整范围，或等待新的文字采集完成。';
    if (/configured|configuration|provider_missing/.test(code)) return '请先在本机配置模型服务。';
    if (/busy/.test(code)) return '本机正在处理另一项请求，请稍后再试。';
    if (/timeout|deadline/.test(code)) return '本次请求等待超时，可以检查服务后重试。';
    return '本次请求没有完成，请检查本机服务与模型设置后重试。';
  }

  function setAiProvider(provider) {
    if (!provider || typeof provider.configured !== 'boolean') throw apiError('invalid_response');
    if (ai.provider?.target_fingerprint !== provider.target_fingerprint) { ai.consentKey = null; ai.cloudConsentAllowed = true; ai.lastTestResult = null; aiNotice(); }
    ai.provider = { base_url: typeof provider.base_url === 'string' ? provider.base_url : '', model: typeof provider.model === 'string' ? provider.model : '', has_key: provider.has_key === true, configured: provider.configured, target_fingerprint: typeof provider.target_fingerprint === 'string' ? provider.target_fingerprint : '', is_local: provider.is_local === true };
    renderAiControls();
    renderModelConnectionStatus();
  }

  function renderAiScopes() {
    const select = $('ai-scope');
    const previous = select.value || 'all';
    const signature = JSON.stringify(conversations.map(row => [row.id, row.name]));
    if (select.dataset.signature !== signature) {
      select.replaceChildren(new Option('全部关注会话', 'all'));
      for (const chat of conversations) select.append(new Option(chat.name, chat.id));
      select.value = [...select.options].some(option => option.value === previous) ? previous : 'all';
      select.dataset.signature = signature;
      if (select.value !== previous) resetAiScope();
    }
    renderAiControls();
  }

  function renderAiKnowledge() {
    if (!ai.initialized) return;
    const previewTotal = conversations.reduce((sum, chat) => sum + chat.messages.length, 0);
    const knowledge = LIVE ? ai.status?.knowledge : { ready: true, conversation_count: conversations.length, message_count: previewTotal, baseline_at: initialDate.toISOString(), last_saved_at: lastUpdate.toISOString() };
    $('ai-knowledge-label').textContent = knowledge ? (LIVE ? '已采文字 · ' : '虚构知识库 · ') + knowledge.conversation_count + ' 个会话 · ' + knowledge.message_count + ' 条' : ai.statusError ? '知识库状态暂不可用' : '正在读取知识库状态';
    $('ai-kb-conversations').textContent = knowledge ? String(knowledge.conversation_count) + ' 个' : '—';
    $('ai-kb-messages').textContent = knowledge ? String(knowledge.message_count) + ' 条' : '—';
    for (const [id, value] of [['ai-kb-baseline', knowledge?.baseline_at], ['ai-kb-last', knowledge?.last_saved_at]]) {
      const date = liveDate(value);
      $(id).textContent = date ? date.toLocaleString('zh-CN', { hour12: false }) + (LIVE ? '' : '（虚构）') : '暂无记录';
    }
    $('ai-kb-state').textContent = !knowledge ? '等待本机状态' : knowledge.ready ? '可检索已采文字' : '等待知识库就绪';
    const identity = knowledge?.direction_identity;
    $('ai-kb-identity').textContent = !LIVE ? '虚构身份示例' : identity?.state === 'verified' && identity.verified === true ? '已自动核验' : '等待本机核验，未确认方向会单独标注';
    $('knowledge-description').textContent = LIVE ? '只覆盖选定会话中，启用后已经采集并保存的文字。' : '这是虚构知识库示例，没有读取真实聊天。';
    $('ai-welcome-boundary').textContent = LIVE ? '仅使用选定会话中启用后已采集的文字。' : '当前为虚构对话预览，不连接模型或真实聊天。';
  }

  function renderAiControls() {
    if (!ai.initialized) return;
    const busy = aiBusy();
    const provider = ai.provider;
    const configured = provider?.configured === true;
    const plan = currentAiPlan();
    $('ai-send').disabled = busy || (LIVE && (!live.connected || ai.status?.busy === true));
    $('ai-send').querySelector('span').textContent = ai.planSubmitting ? '核对范围' : '发送';
    $('ai-question').readOnly = ai.planSubmitting;
    $('ai-time-range').disabled = busy;
    $('ai-scope').disabled = busy;
    $('ai-new-thread').disabled = busy;
    $('ai-model-target').querySelector('span').textContent = LIVE ? configured ? provider.model : '未配置模型' : '虚构预览';
    const cloud = LIVE && configured && !provider.is_local && plan?.requires_model === true;
    $('ai-cloud-consent').hidden = !cloud;
    $('ai-cloud-confirm').disabled = busy;
    // Default on for each chosen scope/target; a manual opt-out survives all
    // renders, plan refreshes and polling until that scope or target changes.
    ai.consentKey = cloud && ai.cloudConsentAllowed ? aiScopeKey() : null;
    $('ai-cloud-confirm').checked = ai.cloudConsentAllowed;
    $('ai-cloud-confirm-label').textContent = '允许将' + (plan ? plan.time_label + '、' + aiPlanScopeLabel(plan) : '本次所选范围') + '内与问题相关的文字片段发送至 ' + aiTarget() + '。';
    document.querySelectorAll('[data-ai-prompt]').forEach(button => { button.disabled = busy; });
    $('ai-runtime').hidden = !ai.job;
    if (ai.job) {
      const stage = ai.job.stage;
      const stages = { submitting: '正在提交问题', querying: '正在查询本机数据', retrieving: '正在检索原消息', generating: '正在生成回答', cancelling: '正在请求取消', connection_error: '任务连接暂时中断' };
      $('ai-stage-text').textContent = (LIVE ? '' : '虚构演示：') + (stages[stage] || '正在处理');
      $('ai-stage-detail').textContent = stage === 'querying' ? '直接查询本机已采文字，不调用模型。' : stage === 'retrieving' ? '只检索你选择的会话与时间范围。' : stage === 'connection_error' ? '已显示内容保留，正在重新读取本机任务状态。' : ai.job.target || (LIVE ? aiTarget() : '没有真实模型请求');
      $('ai-cancel').disabled = !ai.job.id || stage === 'cancelling';
      $('ai-cancel').textContent = stage === 'cancelling' ? '正在取消' : '取消';
    }
    const modelFact = document.querySelectorAll('.property-list dd')[3];
    if (modelFact && LIVE) modelFact.textContent = configured ? '主动提问时使用' : '尚未配置';
    $('ai-bottom-note').textContent = LIVE ? '统计直接查询本机已采文字；分析使用你确认的模型。点击原消息证据核对上下文。' : '虚构交互示例，不代表模型回答或真实采集结果。';
    renderAiPlan();
  }

  function evidenceButton(evidence, compact = false) {
    const button = element('button', compact ? 'ai-inline-citation' : 'ai-evidence-card');
    button.type = 'button';
    button.setAttribute('aria-label', '查看原消息证据 ' + evidence.id);
    if (compact) button.textContent = '[' + evidence.id + ']';
    else {
      const top = element('span', 'ai-evidence-top');
      top.append(element('strong', '', evidence.id + ' · ' + evidence.conversation_name), element('span', '', evidence.sender || '未知发送人'));
      button.append(top, element('span', 'ai-evidence-excerpt', typeof evidence.body === 'string' ? evidence.body.slice(0, 160) : '查看原文与上下文'), element('span', 'ai-evidence-open', '查看原消息 →'));
    }
    button.addEventListener('click', () => openAiEvidence(evidence));
    return button;
  }

  function appendAiText(container, content, evidence = []) {
    const known = new Map(evidence.map(row => [row.id, row]));
    const pattern = /\[(E[1-9][0-9]*)\]/g;
    let cursor = 0;
    for (const match of content.matchAll(pattern)) {
      container.append(document.createTextNode(content.slice(cursor, match.index)));
      container.append(known.has(match[1]) ? evidenceButton(known.get(match[1]), true) : document.createTextNode(match[0]));
      cursor = match.index + match[0].length;
    }
    container.append(document.createTextNode(content.slice(cursor)));
  }

  function renderAiTurns(options = {}) {
    const scroll = $('ai-chat-scroll');
    const previousTop = scroll.scrollTop;
    const follow = options.bottom || (currentView === 'overview' && aiNearBottom());
    const list = $('ai-turns');
    list.replaceChildren();
    $('ai-welcome').hidden = ai.turns.length > 0;
    for (const turn of ai.turns) {
      const row = element('article', 'ai-turn ai-' + turn.role);
      const statistics = turn.response_kind === 'statistics' || turn.plan?.route === 'local_stats';
      const label = turn.role === 'user' ? '你' : turn.role === 'system' ? '系统提示' : statistics ? '本机统计' : LIVE ? 'ChatLens' : 'ChatLens · 虚构回答示例';
      const header = element('div', 'ai-turn-header');
      header.append(element('strong', '', label));
      if (turn.role === 'assistant' && turn.plan?.requires_model) header.append(element('span', 'ai-route-label', aiRouteLabel(turn.plan)));
      if (turn.role === 'assistant' && !statistics && turn.model) header.append(element('span', '', turn.model));
      row.append(header);
      if (turn.role !== 'user' && turn.plan) row.append(element('p', 'ai-turn-plan', aiPlanDescription(turn.plan)));
      const content = element('div', 'ai-answer-text');
      appendAiText(content, turn.content || '', turn.evidence || []);
      row.append(content);
      if (turn.evidence?.length && (turn.content || ['done', 'error'].includes(turn.status))) {
        const cited = new Set([...(turn.content || '').matchAll(/\[(E[1-9][0-9]*)\]/g)].map(match => match[1]));
        const preferred = turn.evidence.filter(item => cited.has(item.id));
        const visible = (preferred.length ? preferred : turn.evidence).slice(0, 4);
        const remaining = turn.evidence.filter(item => !visible.includes(item));
        const source = element('div', 'ai-evidence-list');
        for (const evidence of visible) source.append(evidenceButton(evidence));
        row.append(element('p', 'ai-evidence-label', (preferred.length ? '引用 ' + preferred.length + ' 条原消息' : '检索到 ' + turn.evidence.length + ' 条片段') + (turn.coverage?.truncated ? ' · 当前只覆盖部分相关片段' : '')), source);
        if (remaining.length) {
          const details = element('details', 'ai-other-evidence');
          details.append(element('summary', '', '展开其余 ' + remaining.length + ' 条检索片段'));
          const extra = element('div', 'ai-evidence-list');
          for (const evidence of remaining) extra.append(evidenceButton(evidence));
          details.append(extra);
          row.append(details);
        }
      }
      if (turn.role !== 'user' && turn.status === 'cancelled') row.append(element('p', 'ai-turn-note', '已取消本次请求。'));
      if (turn.role !== 'user' && turn.plan?.warnings?.length) row.append(element('p', 'ai-turn-note', turn.plan.warnings.join(' ')));
      if (turn.role !== 'user' && turn.status === 'done' && turn.coverage?.retrieval_fallback === true) row.append(element('p', 'ai-turn-note ai-retrieval-note', '本轮使用所选范围内的近期片段理解问题，可能只覆盖部分消息。'));
      if (turn.status === 'error' || turn.status === 'connection_error') {
        const feedback = element('div', 'ai-turn-error');
        feedback.append(element('p', '', turn.error || '本次回答没有完成。'));
        const retry = element('button', 'text-button', turn.status === 'connection_error' ? '重新读取任务状态' : '重新提问');
        retry.type = 'button';
        retry.disabled = aiBusy() && turn.status !== 'connection_error';
        retry.addEventListener('click', () => {
          if (turn.status === 'connection_error' && ai.job?.id) { clearTimeout(ai.jobTimer); pollAiJob(ai.jobRequest); }
          else if (!aiBusy()) { $('ai-question').value = turn.question || ''; submitAiQuestion(); }
        });
        feedback.append(retry);
        row.append(feedback);
      }
      if (turn.role === 'assistant' && turn.status === 'done' && !turn.evidence?.length) row.append(element('p', 'ai-turn-note', '本轮未附原消息证据。'));
      list.append(row);
    }
    if (follow) { scroll.scrollTop = scroll.scrollHeight; $('ai-new-answer').hidden = true; }
    else { scroll.scrollTop = previousTop; if (options.updated) $('ai-new-answer').hidden = false; }
    renderAiControls();
  }

  function renderAiThreads() {
    const list = $('ai-thread-list');
    list.replaceChildren();
    for (const thread of ai.threads.slice(0, 8)) {
      const button = element('button', 'ai-history-item' + (thread.id === ai.threadId ? ' active' : ''), thread.title || '新的对话');
      button.type = 'button';
      button.disabled = aiBusy();
      button.setAttribute('aria-pressed', String(thread.id === ai.threadId));
      button.addEventListener('click', () => loadAiThread(thread.id));
      list.append(button);
    }
    if (!ai.threads.length) list.append(element('p', 'ai-history-empty', '还没有对话'));
  }

  async function refreshAiStatus(force = false) {
    if (!LIVE || !live.connected || (!force && Date.now() - ai.statusAt < 5000)) return;
    ai.statusAt = Date.now();
    const request = ++ai.statusRequest;
    try {
      const result = await api('/api/ai/status');
      if (request !== ai.statusRequest) return;
      if (!result?.knowledge || !Number.isSafeInteger(result.knowledge.message_count) || !Number.isSafeInteger(result.knowledge.conversation_count)) throw apiError('invalid_response');
      ai.status = result;
      ai.statusError = false;
      setAiProvider(result.provider);
    } catch { if (request === ai.statusRequest) ai.statusError = true; }
    renderAiKnowledge();
    renderAiControls();
  }

  async function refreshAiThreads() {
    if (!LIVE) { renderAiThreads(); return; }
    try {
      const result = await api('/api/ai/threads');
      if (!Array.isArray(result?.threads)) return;
      ai.threads = result.threads.filter(row => typeof row.id === 'string' && typeof row.title === 'string');
      renderAiThreads();
    } catch { /* Existing answers stay readable when history is unavailable. */ }
  }

  async function loadAiThread(id) {
    if (aiBusy()) return;
    const request = ++ai.threadRequest;
    ai.threadLoading = true;
    renderAiControls();
    try {
      const result = LIVE ? await api('/api/ai/threads/' + encodeURIComponent(id)) : ai.previewThreads.get(id);
      if (request !== ai.threadRequest || !result || !Array.isArray(result.messages)) return;
      ai.threadId = id;
      const latestUser = [...result.messages].reverse().find(row => row.role === 'user');
      if (Array.isArray(latestUser?.scope) && latestUser.scope.length) {
        const candidate = latestUser.scope.length === 1 ? latestUser.scope[0] : 'all';
        $('ai-scope').value = [...$('ai-scope').options].some(option => option.value === candidate) ? candidate : 'all';
      }
      if (['today', '7d', 'all'].includes(latestUser?.time_range)) $('ai-time-range').value = latestUser.time_range;
      const scopeSignature = $('ai-time-range').value + '|' + aiScope().sort().join(',');
      if (scopeSignature !== ai.scopeSignature) { ai.consentKey = null; ai.cloudConsentAllowed = true; }
      ai.scopeSignature = scopeSignature;
      ai.turns = result.messages.filter(row => ['user', 'assistant', 'system'].includes(row.role) && typeof row.content === 'string').map(row => ({ ...row, status: 'done', evidence: cleanAiEvidence(row.evidence), plan: cleanAiPlan(row.plan) }));
      queueAiPlan();
      navigate('overview');
      renderAiTurns({ bottom: true });
      renderAiThreads();
      aiNotice();
    } catch (problem) { aiNotice(aiErrorText(problem)); }
    finally { ai.threadLoading = false; renderAiControls(); renderAiThreads(); }
  }

  function cleanAiEvidence(rows) {
    if (!Array.isArray(rows)) return [];
    return rows.filter(row => row && /^E[1-9][0-9]*$/.test(row.id) && typeof row.conversation_id === 'string' && (Number.isSafeInteger(row.message_id) || (!LIVE && typeof row.message_id === 'string')) && typeof row.conversation_name === 'string' && typeof row.body === 'string');
  }

  function resetAiScope() {
    const signature = $('ai-time-range').value + '|' + aiScope().sort().join(',');
    if (signature === ai.scopeSignature) return;
    const hadConversation = ai.turns.length > 0;
    ai.scopeSignature = signature;
    ai.consentKey = null;
    ai.cloudConsentAllowed = true;
    ai.threadId = null;
    ai.turns = [];
    ai.threadRequest++;
    renderAiTurns();
    renderAiThreads();
    queueAiPlan();
    if (hadConversation) aiNotice('范围已更改，下次提问会开始新的对话。原对话仍可在左侧查看。');
  }

  async function newAiThread() {
    if (aiBusy()) return;
    const request = ++ai.threadRequest;
    ai.threadLoading = true;
    renderAiControls();
    try {
      const result = LIVE ? await aiPost('/api/ai/threads') : { id: 'preview-thread-' + Date.now(), messages: [] };
      if (typeof result?.id !== 'string') throw apiError('invalid_response');
      if (request !== ai.threadRequest) return;
      ai.threadId = result.id;
      ai.turns = [];
      queueAiPlan();
      aiNotice();
      renderAiTurns({ bottom: true });
      await refreshAiThreads();
      $('ai-question').focus();
    } catch (problem) { aiNotice(aiErrorText(problem)); }
    finally { ai.threadLoading = false; renderAiControls(); renderAiThreads(); }
  }

  async function submitAiQuestion() {
    const question = $('ai-question').value.trim();
    if (!question || aiBusy()) return;
    if (LIVE && !live.connected) { aiNotice('请先恢复与本机服务的连接。'); return; }
    if (LIVE && ai.status?.busy) { aiNotice('上一项请求仍在结束，请稍后再提问。'); return; }
    if (!aiScope().length) { aiNotice('请先启用需要关注的会话。'); return; }
    let plan = null;
    if (LIVE) {
      ai.planSubmitting = true;
      renderAiControls();
      try {
        plan = await requestAiPlan();
        if (!plan) { aiNotice('问题或范围已经变化，请确认后重新发送。'); return; }
      } catch (problem) { aiNotice(aiErrorText(problem)); return; }
      finally { ai.planSubmitting = false; renderAiControls(); }
      if (plan.requires_model && !ai.provider?.configured) { aiNotice('这个问题需要模型分析，请先在设置中配置模型。问题草稿会保留。'); navigate('settings'); return; }
      if (plan.requires_model && (!ai.status?.knowledge?.ready || ai.status.knowledge.message_count === 0)) { aiNotice('知识库中还没有可供分析的文字。请等待选定会话的新文字采集完成。'); return; }
      if (plan.requires_model && !ai.provider.is_local && ai.consentKey !== aiScopeKey()) { aiNotice('请核对本轮实际范围与模型目标，再勾选允许发送相关文字片段。'); $('ai-cloud-confirm').focus(); return; }
    }
    const payload = aiPlanInput();
    const user = { role: 'user', content: question, status: 'done', plan };
    ai.threadRequest++;
    const target = LIVE ? plan.requires_model ? aiTarget() : aiRouteLabel(plan) : '没有真实模型请求';
    const answer = { role: 'assistant', content: '', status: 'working', evidence: [], question, model: LIVE && !plan.requires_model ? null : target, plan };
    ai.turns.push(user, answer);
    const request = ++ai.jobRequest;
    ai.job = { id: null, stage: 'submitting', turn: answer, target };
    aiNotice();
    renderAiTurns({ bottom: true });
    renderAiThreads();
    if (!LIVE) {
      if ($('ai-question').value.trim() === question) $('ai-question').value = '';
      ai.job.id = 'preview-job';
      ai.job.stage = 'retrieving';
      renderAiControls();
      ai.jobTimer = setTimeout(() => {
        if (request !== ai.jobRequest || !ai.job) return;
        ai.job.stage = 'generating';
        renderAiControls();
        ai.jobTimer = setTimeout(() => finishPreviewAi(answer, request), 600);
      }, 500);
      return;
    }
    try {
      const result = await aiPost('/api/ai/questions', { ...payload, ...(plan.requires_model ? { target_fingerprint: ai.provider.target_fingerprint, ...(!ai.provider.is_local ? { confirmed_target: ai.consentKey === aiScopeKey() ? ai.provider.target_fingerprint : '' } : {}) } : {}) });
      if (request !== ai.jobRequest || !ai.job) return;
      if (typeof result?.job_id !== 'string' || typeof result.thread_id !== 'string') throw apiError('invalid_response');
      ai.threadId = result.thread_id;
      ai.job.id = result.job_id;
      ai.job.stage = plan.requires_model ? 'retrieving' : 'querying';
      if ($('ai-question').value.trim() === question) { $('ai-question').value = ''; queueAiPlan(); }
      renderAiControls();
      pollAiJob(request);
    } catch (problem) {
      if (request !== ai.jobRequest) return;
      answer.status = 'error';
      answer.error = aiErrorText(problem);
      ai.job = null;
      renderAiTurns({ updated: true });
    }
  }

  function finishPreviewAi(answer, request) {
    if (request !== ai.jobRequest || !ai.job) return;
    const chat = conversations[0];
    const first = chat.messages[6];
    const second = chat.messages[8];
    answer.content = '这是虚构回答示例，用来确认对话和证据阅读体验，没有连接 AI 模型。\n\n示例中，客户确认了订单数量 [E1]，并提出了交付时间要求 [E2]。你可以点击证据，回到虚构原消息核对。\n\n真实使用时，回答只会依据你选择范围内已经采集的文字。';
    answer.evidence = [first, second].map((message, index) => ({ id: 'E' + (index + 1), message_id: message.id, conversation_id: chat.id, conversation_name: chat.name, sender: message.sender, body: message.body, sent_at: Math.floor(message.date.getTime() / 1000) }));
    answer.status = 'done';
    ai.job = null;
    if (!ai.threadId) ai.threadId = 'preview-thread-' + Date.now();
    ai.previewThreads.set(ai.threadId, { id: ai.threadId, messages: ai.turns.map(row => ({ ...row })) });
    const title = ai.turns.find(row => row.role === 'user')?.content.slice(0, 32) || '虚构对话';
    ai.threads = [{ id: ai.threadId, title }, ...ai.threads.filter(row => row.id !== ai.threadId)];
    renderAiTurns({ updated: true });
    renderAiThreads();
    announce('虚构回答示例已显示，没有调用模型。');
  }

  async function pollAiJob(request) {
    if (request !== ai.jobRequest || !ai.job?.id) return;
    const job = ai.job;
    try {
      const result = await api('/api/ai/jobs/' + encodeURIComponent(job.id));
      if (request !== ai.jobRequest || ai.job !== job) return;
      if (!['querying', 'retrieving', 'generating', 'done', 'error', 'cancelled'].includes(result?.stage)) throw apiError('invalid_response');
      const turn = job.turn;
      const plan = cleanAiPlan(result.plan);
      if (plan) turn.plan = plan;
      if (result.response_kind === 'system') { turn.role = 'system'; turn.model = null; }
      if (result.response_kind === 'statistics') { turn.role = 'assistant'; turn.response_kind = 'statistics'; turn.model = null; }
      const changed = typeof result.answer === 'string' && turn.content !== result.answer;
      if (typeof result.answer === 'string') turn.content = result.answer;
      if (Array.isArray(result.evidence)) turn.evidence = cleanAiEvidence(result.evidence);
      if (result.coverage && typeof result.coverage === 'object') turn.coverage = result.coverage;
      job.stage = result.stage;
      turn.error = null;
      turn.status = ['done', 'error', 'cancelled'].includes(result.stage) ? result.stage : 'working';
      if (result.stage === 'error') turn.error = aiErrorText(result.error);
      if (result.stage === 'done' && !turn.content) { turn.role = 'system'; turn.content = '本轮没有生成回答。所选范围内可能没有足够的原消息证据，请调整范围后再试。'; }
      if (['done', 'error', 'cancelled'].includes(result.stage)) {
        ai.job = null;
        refreshAiThreads();
        refreshAiStatus(true);
      }
      renderAiTurns({ updated: changed || result.stage === 'done' });
      renderAiThreads();
      if (ai.job) ai.jobTimer = setTimeout(() => pollAiJob(request), 700);
    } catch (problem) {
      if (request !== ai.jobRequest || ai.job !== job) return;
      job.stage = 'connection_error';
      job.turn.status = 'connection_error';
      job.turn.error = problem.status === 401 ? aiErrorText(problem) : '暂时无法读取任务状态。已显示内容保留，重新连接只读取原任务，不会重复提问。';
      renderAiTurns();
      if (problem.status !== 401) ai.jobTimer = setTimeout(() => pollAiJob(request), 2500);
    }
  }

  async function cancelAiJob() {
    if (!ai.job?.id) return;
    const job = ai.job;
    clearTimeout(ai.jobTimer);
    if (!LIVE) {
      ai.jobRequest++;
      job.turn.status = 'cancelled';
      ai.job = null;
      renderAiTurns();
      renderAiThreads();
      return;
    }
    job.stage = 'cancelling';
    renderAiControls();
    try {
      await aiPost('/api/ai/jobs/' + encodeURIComponent(job.id) + '/cancel');
      if (ai.job === job) pollAiJob(ai.jobRequest);
    } catch (problem) {
      if (ai.job === job) {
        job.stage = 'connection_error';
        job.turn.status = 'connection_error';
        job.turn.error = '取消请求暂未确认。请重新读取任务状态，或恢复本机服务后再取消。';
        renderAiTurns();
      }
    }
  }

  async function openAiEvidence(evidence) {
    const request = ++ai.evidenceRequest;
    const sourceView = currentView;
    if (!LIVE) {
      $('conversation-search').value = '';
      setFilter('all');
      selectConversation(evidence.conversation_id);
      highlightEvidence(evidence.message_id);
      $('citation-context').hidden = false;
      $('citation-context-label').textContent = '正在查看虚构原消息证据';
      return;
    }
    try {
      const query = new URLSearchParams({ conversation_id: evidence.conversation_id, message_id: String(evidence.message_id) });
      const result = await api('/api/ai/evidence?' + query);
      if (request !== ai.evidenceRequest || currentView !== sourceView) return;
      const page = decodePage({ ...result, has_more: false }, evidence.conversation_id);
      if (result.target_id !== evidence.message_id || !page.messages.some(row => row.id === evidence.message_id)) throw apiError('invalid_response');
      $('conversation-search').value = '';
      setFilter('all');
      selectLiveConversation(evidence.conversation_id, { context: { chatId: evidence.conversation_id, targetId: evidence.message_id, messages: page.messages } });
      $('message-search').value = '';
      renderMessages();
      $('citation-context').hidden = false;
      $('citation-context-label').textContent = '正在查看引用原文附近 · ' + page.messages.length + ' 条';
      highlightEvidence(evidence.message_id);
    } catch { toast('原消息暂时无法定位，请在消息页查看当前会话。'); }
  }

  function highlightEvidence(id) {
    requestAnimationFrame(() => {
      const row = [...$('message-list').querySelectorAll('[data-message-id]')].find(node => node.dataset.messageId === String(id));
      if (!row) return;
      row.classList.add('is-evidence-target');
      row.setAttribute('tabindex', '-1');
      row.scrollIntoView({ block: 'center', behavior: 'instant' });
      row.focus({ preventScroll: true });
    });
  }

  const modelPresets = { 'minimax-cn': 'https://api.minimax.cn/v1', 'minimax-global': 'https://api.minimax.io/v1' };
  function modelFeedback(text = '', failure = false) { $('model-feedback').hidden = !text; $('model-feedback').textContent = text; $('model-feedback').classList.toggle('is-error', failure); }
  function modelDirty() { return $('model-base-url').value.trim() !== (ai.provider?.base_url || '') || $('model-name').value.trim() !== (ai.provider?.model || '') || Boolean($('model-api-key').value) || $('model-delete-key').checked; }
  function renderModelConnectionStatus() {
    const badge = $('model-connection-status');
    if (!badge) return;
    const result = ai.lastTestResult?.fingerprint === ai.provider?.target_fingerprint ? ai.lastTestResult : null;
    const label = !LIVE ? '虚构预览' : ai.testJob ? '正在测试' : ai.modelBusy ? '正在处理' : result?.stage === 'done' ? '测试通过' : result?.stage === 'error' ? '测试未通过' : ai.provider?.configured ? '已配置' : '未配置';
    badge.textContent = label;
    badge.className = 'tag tag-' + (ai.testJob || ai.modelBusy ? 'running' : result?.stage === 'error' ? 'failure' : result?.stage === 'done' ? 'success' : 'neutral');
  }
  function renderModelControls() {
    const busy = ai.modelBusy || Boolean(ai.testJob);
    for (const id of ['model-preset', 'model-base-url', 'model-name', 'model-delete-key']) $(id).disabled = busy;
    $('model-api-key').disabled = busy || $('model-delete-key').checked;
    $('model-save').disabled = busy || Boolean(ai.job);
    $('model-save').classList.toggle('is-loading', ai.modelBusy);
    $('model-test').disabled = busy || Boolean(ai.job) || !ai.provider?.configured || modelDirty();
    $('model-cancel-test').hidden = !ai.testJob;
    const draft = { base_url: $('model-base-url').value.trim(), model: $('model-name').value.trim() };
    $('model-target-preview').textContent = aiTarget(draft);
    renderModelConnectionStatus();
    renderAiControls();
  }

  function clearModelDraft() {
    $('model-api-key').value = '';
    $('model-delete-key').checked = false;
  }

  function leaveSettings() {
    clearModelDraft();
    ai.modelFormRequest = (ai.modelFormRequest || 0) + 1;
    if (ai.modelAction === 'load') { ai.modelBusy = false; ai.modelAction = null; }
    if (ai.modelAction === 'test') ai.cancelTestOnLeave = true;
    if (ai.testJob) cancelModelTest();
    renderAiControls();
  }

  async function loadModelSettings() {
    if (ai.modelBusy || ai.testJob || currentView !== 'settings') { renderModelControls(); return; }
    const request = ai.modelFormRequest = (ai.modelFormRequest || 0) + 1;
    clearModelDraft();
    $('model-base-url').value = ai.provider?.base_url || modelPresets['minimax-cn'];
    $('model-name').value = ai.provider?.model || 'MiniMax-M3.1-Flash';
    ai.modelBusy = LIVE && live.connected;
    ai.modelAction = ai.modelBusy ? 'load' : null;
    renderModelControls();
    modelFeedback(ai.modelBusy ? '正在读取本机配置…' : '');
    let readFailed = false;
    if (LIVE && live.connected) {
      try { const result = await api('/api/ai/config'); if (request !== ai.modelFormRequest || currentView !== 'settings') return; setAiProvider(result.provider || result); }
      catch (problem) { if (request !== ai.modelFormRequest || currentView !== 'settings') return; readFailed = true; modelFeedback(aiErrorText(problem), true); }
    }
    if (request !== ai.modelFormRequest || currentView !== 'settings') return;
    const provider = ai.provider;
    $('model-base-url').value = provider?.base_url || modelPresets['minimax-cn'];
    $('model-name').value = provider?.model || 'MiniMax-M3.1-Flash';
    $('model-preset').value = Object.entries(modelPresets).find(([, url]) => url === $('model-base-url').value)?.[0] || 'custom';
    $('model-delete-key-row').hidden = !provider?.has_key;
    $('model-api-key').placeholder = provider?.has_key ? '已配置 · 留空保留' : '在本机输入 API Key';
    $('model-key-help').textContent = provider?.has_key ? '已保存本机密钥。留空会保留原密钥，输入新值会替换。' : '请在本机输入密钥，不要发送到聊天里。';
    if (!LIVE) modelFeedback('虚构预览不会保存真实配置，也不会连接模型。');
    else if (!readFailed) modelFeedback();
    ai.modelBusy = false;
    ai.modelAction = null;
    renderModelControls();
  }

  async function saveModel(event) {
    event.preventDefault();
    if (aiBusy()) return;
    if (!LIVE) { $('model-api-key').value = ''; modelFeedback('这是虚构预览。没有保存配置或调用模型。'); renderModelControls(); return; }
    ai.modelBusy = true;
    ai.modelAction = 'save';
    modelFeedback('正在保存到本机…');
    renderModelControls();
    try {
      const deleting = $('model-delete-key').checked;
      const key = $('model-api-key').value;
      const result = await aiPost('/api/ai/config', { base_url: $('model-base-url').value.trim(), model: $('model-name').value.trim(), key_action: deleting ? 'delete' : key ? 'replace' : 'keep', ...(key && !deleting ? { api_key: key } : {}) });
      setAiProvider(result.provider || result);
      $('model-base-url').value = ai.provider.base_url;
      $('model-name').value = ai.provider.model;
      $('model-api-key').value = '';
      $('model-delete-key').checked = false;
      $('model-delete-key-row').hidden = !ai.provider.has_key;
      $('model-api-key').placeholder = ai.provider.has_key ? '已配置 · 留空保留' : '在本机输入 API Key';
      $('model-key-help').textContent = ai.provider.has_key ? '本机密钥已保存，输入框已清空。留空会保留原密钥。' : '当前没有已保存的密钥。';
      modelFeedback('已保存到本机。尚未测试连接，也没有发送聊天。');
      aiNotice();
      refreshAiStatus(true);
    } catch (problem) { modelFeedback(aiErrorText(problem), true); }
    finally { $('model-api-key').value = ''; ai.modelBusy = false; ai.modelAction = null; renderModelControls(); }
  }

  async function testModel() {
    if (aiBusy() || modelDirty() || !ai.provider?.configured) return;
    if (!LIVE) { modelFeedback('虚构预览不会连接模型。'); return; }
    ai.modelBusy = true;
    ai.modelAction = 'test';
    ai.cancelTestOnLeave = false;
    modelFeedback('正在请求测试 ' + aiTarget() + '，不发送聊天内容。');
    renderModelControls();
    try {
      const result = await aiPost('/api/ai/test', { target_fingerprint: ai.provider.target_fingerprint });
      if (typeof result?.job_id !== 'string') throw apiError('invalid_response');
      ai.testJob = { id: result.job_id, target: aiTarget(), fingerprint: ai.provider.target_fingerprint };
      if (ai.cancelTestOnLeave || currentView !== 'settings') cancelModelTest();
      else pollModelTest();
    } catch (problem) { modelFeedback(aiErrorText(problem), true); }
    finally { ai.modelBusy = false; ai.modelAction = null; renderModelControls(); }
  }

  async function pollModelTest() {
    const job = ai.testJob;
    if (!job) return;
    try {
      const result = await api('/api/ai/jobs/' + encodeURIComponent(job.id));
      if (ai.testJob !== job) return;
      if (result.stage === 'done') { ai.lastTestResult = { fingerprint: job.fingerprint, stage: 'done' }; modelFeedback('连接测试通过：' + job.target + '。没有发送聊天内容。'); ai.testJob = null; }
      else if (result.stage === 'error') { ai.lastTestResult = { fingerprint: job.fingerprint, stage: 'error' }; modelFeedback(aiErrorText(result.error), true); ai.testJob = null; }
      else if (result.stage === 'cancelled') { ai.lastTestResult = { fingerprint: job.fingerprint, stage: 'cancelled' }; modelFeedback('已取消连接测试。'); ai.testJob = null; }
      else { modelFeedback('正在测试 ' + job.target + '…'); ai.testTimer = setTimeout(pollModelTest, 700); }
    } catch (problem) { if (ai.testJob === job) { modelFeedback(aiErrorText(problem), true); ai.testJob = null; } }
    renderModelControls();
  }

  async function cancelModelTest() {
    const job = ai.testJob;
    if (!job) return;
    clearTimeout(ai.testTimer);
    try { await aiPost('/api/ai/jobs/' + encodeURIComponent(job.id) + '/cancel'); }
    catch { modelFeedback('取消请求暂未确认，请检查本机服务。', true); }
    if (ai.testJob === job) pollModelTest();
  }

  function initializeAi() {
    ai.initialized = true;
    $('ai-form').addEventListener('submit', event => { event.preventDefault(); submitAiQuestion(); });
    $('ai-question').addEventListener('keydown', event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); submitAiQuestion(); } });
    $('ai-question').addEventListener('input', queueAiPlan);
    $('ai-scope').addEventListener('change', resetAiScope);
    $('ai-time-range').addEventListener('change', resetAiScope);
    $('ai-cloud-confirm').addEventListener('change', () => { ai.cloudConsentAllowed = $('ai-cloud-confirm').checked; ai.consentKey = ai.cloudConsentAllowed ? aiScopeKey() : null; });
    $('ai-new-thread').addEventListener('click', newAiThread);
    $('ai-cancel').addEventListener('click', cancelAiJob);
    $('ai-new-answer').addEventListener('click', () => { $('ai-chat-scroll').scrollTop = $('ai-chat-scroll').scrollHeight; $('ai-new-answer').hidden = true; });
    $('ai-chat-scroll').addEventListener('scroll', () => { if (aiNearBottom()) $('ai-new-answer').hidden = true; }, { passive: true });
    const prompts = { today: '今天有哪些重点？', followup: '最近有哪些客户需要跟进？', agreement: '最近有哪些约定？', risk: '最近有没有需要注意的风险？' };
    document.querySelectorAll('[data-ai-prompt]').forEach(button => button.addEventListener('click', () => {
      if (aiBusy()) return;
      if (button.dataset.aiPrompt === 'today' && $('ai-time-range').value !== 'today') { $('ai-time-range').value = 'today'; resetAiScope(); }
      $('ai-question').value = prompts[button.dataset.aiPrompt];
      queueAiPlan();
      $('ai-question').focus();
    }));
    $('ai-knowledge-toggle').addEventListener('click', () => $('knowledge-dialog').showModal());
    $('close-knowledge').addEventListener('click', () => $('knowledge-dialog').close());
    $('knowledge-open-messages').addEventListener('click', () => { $('knowledge-dialog').close(); navigate('messages'); });
    $('model-form').addEventListener('submit', saveModel);
    $('model-test').addEventListener('click', testModel);
    $('model-cancel-test').addEventListener('click', cancelModelTest);
    $('model-preset').addEventListener('change', () => { const preset = modelPresets[$('model-preset').value]; if (preset) { $('model-base-url').value = preset; $('model-name').value = 'MiniMax-M3.1-Flash'; } modelFeedback(); renderModelControls(); });
    for (const id of ['model-base-url', 'model-name', 'model-api-key', 'model-delete-key']) $(id).addEventListener('input', () => { if ($('model-delete-key').checked) $('model-api-key').value = ''; modelFeedback(); renderModelControls(); });
    for (const id of ['knowledge-dialog']) {
      $(id).addEventListener('click', event => { if (event.target === $(id)) $(id).close(); });
      $(id).addEventListener('close', () => {
        if (window.innerWidth <= 640) $('nav-toggle').focus({ preventScroll: true });
      });
    }
    $('citation-back-ai').addEventListener('click', () => navigate('overview'));
    $('citation-latest').addEventListener('click', () => { live.evidence = null; $('citation-context').hidden = true; if (LIVE) jumpLiveLatest(); else { renderMessages({ bottom: true }); } });
    renderAiScopes();
    renderAiKnowledge();
    renderAiTurns();
    renderAiThreads();
  }

  $('conversation-search').addEventListener('input', renderConversations);
  $('message-search').addEventListener('input', () => {
    if (LIVE) { live.evidence = null; $('citation-context').hidden = true; requestLiveSearch(); }
    else renderMessages({ restore: 0 });
  });
  function setFilter(value) {
    typeFilter = value;
    document.querySelectorAll('.filter-button').forEach(item => {
      item.classList.toggle('active', item.dataset.filter === value);
      item.setAttribute('aria-pressed', String(item.dataset.filter === value));
    });
    renderConversations();
  }
  document.querySelectorAll('.filter-button').forEach(button => button.addEventListener('click', () => setFilter(button.dataset.filter)));
  document.querySelectorAll('[data-nav]').forEach(button => button.addEventListener('click', () => navigate(button.dataset.nav)));
  document.querySelectorAll('[data-open-view]').forEach(button => button.addEventListener('click', () => navigate(button.dataset.openView)));
  $('brand-home').addEventListener('click', event => { event.preventDefault(); navigate('overview'); });
  $('simulate-message').addEventListener('click', simulateMessage);
  ['toggle-collection', 'collection-toggle'].forEach(id => $(id).addEventListener('click', toggleCollection));
  ['connection-demo', 'collection-connection'].forEach(id => $(id).addEventListener('click', toggleConnection));
  $('new-messages').addEventListener('click', () => {
    if (LIVE) { jumpLiveLatest(); return; }
    if ($('message-search').value) { $('message-search').value = ''; renderMessages({ bottom: true }); }
    scrollToBottom();
  });
  $('message-scroll').addEventListener('scroll', () => {
    if (atBottom() && !$('message-search').value.trim()) { pending.set(activeId, 0); renderNewMessageButton(); }
  }, { passive: true });
  $('toggle-details').addEventListener('click', () => setDetails($('toggle-details').getAttribute('aria-expanded') !== 'true'));
  $('close-details').addEventListener('click', () => { setDetails(false); $('toggle-details').focus(); });
  $('mobile-back').addEventListener('click', () => { $('workbench').classList.remove('mobile-thread-open'); setDetails(false); });
  $('preview-state').addEventListener('change', event => previewState(event.target.value));
  $('retry-preview').addEventListener('click', () => previewState('loading'));
  $('restore-content').addEventListener('click', () => previewState('normal'));
  $('reset-preview').addEventListener('click', () => { reset(); previewState('normal'); toast('已还原虚构示例，未更改真实数据。'); });
  $('nav-toggle').addEventListener('click', () => setDrawer(true));
  $('close-nav').addEventListener('click', () => setDrawer(false));
  $('sidebar-backdrop').addEventListener('click', () => setDrawer(false));
  const dialog = $('preview-dialog');
  $('preview-help').addEventListener('click', () => { setDrawer(false, false); dialog.showModal(); });
  $('close-preview').addEventListener('click', () => dialog.close());
  $('confirm-preview').addEventListener('click', () => dialog.close());
  dialog.addEventListener('click', event => { if (event.target === dialog) dialog.close(); });
  $('settings-form').addEventListener('submit', event => {
    event.preventDefault();
    preferences.density = $('pref-density').value;
    preferences.autoScroll = $('pref-autoscroll').checked;
    document.body.classList.toggle('compact', preferences.density === 'compact');
    toast('显示偏好已应用到本页，刷新后还原。');
  });
  [dialog].forEach(modal => modal.addEventListener('close', () => {
    if (window.innerWidth <= 640) $('nav-toggle').focus({ preventScroll: true });
  }));
  window.addEventListener('pagehide', () => { clearModelDraft(); if (LIVE && live.scopeGeneration) invalidateScope(false); });
  window.addEventListener('pageshow', event => { if (!event.persisted) return; if (LIVE && (live.scopeInvalid || live.scopeGeneration)) { invalidateScope(); return; } if (currentView === 'settings') loadModelSettings(); });
  document.addEventListener('keydown', event => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLocaleLowerCase() === 'k' && !dialog.open && !$('knowledge-dialog').open) {
      event.preventDefault(); navigate('messages'); $('workbench').classList.remove('mobile-thread-open'); $('conversation-search').focus();
    }
    if (event.key === 'Escape' && !dialog.open && !$('knowledge-dialog').open) {
      if (document.body.classList.contains('nav-open')) setDrawer(false);
      else if (currentView === 'messages') setDetails(false);
    }
    if (event.key === 'Tab' && document.body.classList.contains('nav-open')) {
      const focusable = [...$('sidebar').querySelectorAll('a,button')].filter(node => node.offsetParent !== null);
      const first = focusable[0], last = focusable.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });
  window.addEventListener('resize', () => {
    const mobile = window.innerWidth <= 640;
    if (mobile !== wasMobile) setDrawer(false, false);
    wasMobile = mobile;
    const visible = window.innerWidth > 1250 ? !$('workbench').classList.contains('details-hidden') : $('workbench').classList.contains('details-open');
    $('toggle-details').setAttribute('aria-expanded', String(visible));
  });
  setInterval(() => {
    if (LIVE) { updateLiveElapsed(); return; }
    const seconds = Math.floor((Date.now() - demoStartedAt) / 1000);
    $('demo-elapsed').textContent = String(Math.floor(seconds / 60)).padStart(2, '0') + ':' + String(seconds % 60).padStart(2, '0');
  }, 1000);
  $('search-shortcut').textContent = /Mac|iPhone|iPad/.test(navigator.platform) ? '⌘ K' : 'Ctrl K';
  initializeAi();
  if (LIVE) initializeLive();
  else reset();
  navigate('overview');
  setDrawer(false, false);
  if (window.innerWidth <= 1250) setDetails(false);
})();
