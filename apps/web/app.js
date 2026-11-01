/* ChatLens: file:// is fictional preview; loopback HTTP reads the authenticated local service. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const svgNS = 'http://www.w3.org/2000/svg';
  const initialDate = new Date();
  const LIVE = location.protocol !== 'file:';
  const live = { connected: false, ready: false, error: null, status: null, session: null, timer: null, failures: 0, cycle: 0, selection: 0, overview: 0, search: null, searchRequest: 0, searching: false };
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

  function seedMessage(chat, index, sender, body, hour, minute, day = 0) {
    const date = new Date(initialDate);
    date.setDate(date.getDate() + day);
    date.setHours(hour, minute, 0, 0);
    return { id: chat + '-' + index, sender, body, date, own: sender === '我' };
  }

  function makeData() {
    const rows = [
      { id: 'lin', name: '林女士 · 澄禾商贸', short: '林', type: 'private', color: 'sage', subtitle: '私聊 · 虚构客户会话', unread: 0,
        messages: [
          ['林女士', '上午好，上一批样品已经收到了，门店反馈很不错。', 9, 12],
          ['我', '早上好！很高兴样品合适，新门店的数量确定了吗？', 9, 15],
          ['林女士', '目前有两家店要一起上，想先订 800 套。', 9, 18],
          ['林女士', '另外包装上能加上我们自己的门店名称吗？', 9, 20],
          ['我', '可以的，包装文字确认后我们再安排打样。', 9, 23],
          ['林女士', '好的，我今天把设计稿发给你们。', 9, 25],
          ['林女士', '数量确认了：浅灰色 500 套，米白色 300 套。\n麻烦按这个数量更新一下报价。', 10, 32],
          ['我', '收到，我按新的数量重新核算，稍后发你。', 10, 36],
          ['林女士', '这批我们希望下周五之前到店，能帮忙确认一下排期吗？', 10, 42],
          ['林女士', '如果时间来得及，我们今天就可以走定金流程。', 10, 48]
        ] },
      { id: 'star', name: '星桥项目协作群', short: '星桥', type: 'group', color: 'sand', subtitle: '群聊 · 虚构项目沟通', unread: 2,
        messages: [
          ['许设计', '早，首页的第二版稿件已经整理好了。', 9, 10],
          ['陈经理', '我看了一下，新版留白舒服很多。', 9, 14],
          ['我', '下午我们把剩下两处文案过一遍。', 9, 19],
          ['许设计', '可以，产品介绍的图也会一起补上。', 9, 30],
          ['陈经理', '客户希望周四能看到一个完整的预览版本。', 10, 8],
          ['我', '收到，先按周四安排，今天把反馈收齐。', 10, 12],
          ['许设计', '还有一处想确认：品牌色继续沿用上一版的墨绿色吗？', 10, 37],
          ['陈经理', '对，品牌色保持一致，按钮可以再柔和一点。', 10, 51],
          ['许设计', '明白，更新好后我会在群里同步。', 10, 53]
        ] },
      { id: 'zhou', name: '周先生 · 远山工作室', short: '周', type: 'private', color: 'blue', subtitle: '私聊 · 虚构合作会话', unread: 1,
        messages: [
          ['周先生', '你好，想了解一下你们的长期合作方案。', 9, 5],
          ['我', '你好，我们可以先交流一下具体的使用场景。', 9, 9],
          ['周先生', '主要是给我们工作室的客户做定制，每个月大约 200 到 300 份。', 9, 16],
          ['我', '了解。你们对交付周期有什么要求？', 9, 22],
          ['周先生', '通常两周左右，偶尔会有比较急的项目。', 9, 27],
          ['我', '我整理一份按月合作的方案，今天晚些时候发你。', 9, 32],
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
    const messages = searched || chat.messages.filter((message) => !query || (message.body + ' ' + message.sender).toLocaleLowerCase().includes(lowered));
    const list = $('message-list');
    list.replaceChildren();
    let previousDay = '';
    for (const message of messages) {
      const key = message.date ? message.date.toDateString() : 'unknown';
      if (key !== previousDay) {
        list.append(element('div', 'day-divider', dayLabel(message.date)));
        previousDay = key;
      }
      const row = element('article', 'message-row' + (message.own ? ' own' : '') + (message.id === options.newId ? ' new-arrival' : ''));
      row.setAttribute('data-message-id', message.id);
      const avatar = element('div', 'avatar ' + (message.own ? 'own' : chat.color), message.own ? '我' : message.sender.slice(0, 1));
      avatar.setAttribute('aria-hidden', 'true');
      const content = element('div', 'message-content');
      const meta = element('div', 'message-meta');
      const timestamp = element('time', '', timeText(message.date));
      if (message.date) timestamp.dateTime = message.date.toISOString();
      meta.append(element('span', 'message-sender', message.sender), timestamp);
      const body = element('p', 'message-body');
      appendHighlighted(body, message.body, query);
      content.append(meta, body);
      row.append(avatar, content);
      list.append(row);
    }
    $('message-count').textContent = query ? (live.searching && LIVE ? '查找中…' : messages.length + ' 条结果') : (LIVE ? (chat.count || 0) : chat.messages.length) + ' 条';
    if (!messages.length) {
      if (LIVE && (live.searching || liveCache.get(chat.id)?.loading)) list.append(emptyState('正在读取消息', '正在从本机采集库读取已保存的文字。', 'refresh'));
      else list.append(emptyState(query ? '没有找到相关消息' : '等待新的文字消息', query ? (LIVE ? '换个关键词试试。搜索仅限当前所选会话。' : '换个关键词试试。搜索仅限当前虚构会话。') : '启用采集后收到的新文字会显示在这里。', query ? 'search' : 'messages'));
    }
    if (LIVE) updateOlderButton();
    if (options.bottom) {
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
    const message = { id: 'preview-new-' + sequence++, sender: chat.type === 'group' ? '许设计' : (chat.id === 'lin' ? '林女士' : '周先生'), body, date: new Date(Math.max(Date.now(), latestTime + 60000)), own: false };
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
    if ($('overview-scope-count')) $('overview-scope-count').replaceChildren(document.createTextNode(String(conversations.length)), element('small', '', ' 个'));
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
    $('overview-count').replaceChildren(document.createTextNode(String(total)), element('small', '', ' 条'));
    $('overview-state').textContent = state.short;
    $('overview-state').style.color = 'var(--' + state.color + '-text)';
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
    const list = $('recent-conversations');
    list.replaceChildren();
    const sorted = [...conversations].sort((a, b) => (LIVE ? b.latestDate || 0 : b.messages.at(-1)?.date || 0) - (LIVE ? a.latestDate || 0 : a.messages.at(-1)?.date || 0));
    for (const chat of sorted) {
      const last = LIVE ? { date: chat.latestDate, body: chat.latestPreview || '尚未采集到新文字' } : chat.messages.at(-1);
      const row = element('button', 'recent-row');
      row.type = 'button';
      row.setAttribute('aria-label', '打开' + chat.name);
      const copy = element('span', 'recent-copy');
      copy.append(element('strong', '', chat.name), element('span', '', last.body.replace(/\n/g, ' ')));
      row.append(element('span', 'avatar ' + chat.color, chat.short), copy, element('time', '', timeText(last.date)), icon('chevron'));
      row.addEventListener('click', () => selectConversation(chat.id));
      list.append(row);
    }
    if (!sorted.length) list.append(emptyState(LIVE ? '等待采集会话' : '暂无会话', LIVE ? '只会展示本机已授权的会话，连接中不会填入虚构数据。' : '请查看关注的会话。', 'messages'));
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
    if (!['overview', 'messages', 'collection'].includes(view)) return;
    const changed = currentView !== view;
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
    $('breadcrumb-current').textContent = { overview: '沟通总览', messages: '会话消息', collection: '采集状态' }[view];
    setDrawer(false, false);
    if (view === 'messages' && changed) {
      if (LIVE && activeChat().id && !liveCache.get(activeId)?.loaded) ensureLiveMessages(activeId);
      renderMessages({ bottom: !scrollPositions.has(activeId), restore: scrollPositions.get(activeId) });
      renderNewMessageButton();
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

  async function api(route, token) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch(new URL(route, location.origin), {
        credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
        headers: token ? { Authorization: 'Bearer ' + token } : {}
      });
      if (!response.ok) throw apiError('http_' + response.status, response.status);
      try { return await response.json(); }
      catch { throw apiError('invalid_response'); }
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
      return { id: row.id, sender: row.sender, body: row.body, date, observed: liveDate(row.observed_at), own: row.own === true };
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
      const reading = row.id === activeId && currentView === 'messages' && !$('message-search').value.trim() && atBottom() && preferences.autoScroll;
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
    renderRecent();
    renderNewMessageButton();
    document.querySelector('.entry-footer > span').textContent = '在已选的 ' + rows.length + ' 个会话内查找';
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

  function stateDetails() {
    if (live.error) return [live.error.status === 401 ? '本机会话已失效' : '页面连接中断', 'failure', live.error.status === 401 ? '请从本机启动器重新打开工作台。已显示内容保留，暂不更新。' : '暂时无法连接本机服务。已显示内容可能已过期，恢复连接后会继续读取。'];
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
    $('overview-count').replaceChildren(document.createTextNode(String(total)), element('small', '', ' 条'));
    $('collection-total').replaceChildren(document.createTextNode(String(total)), element('small', '', ' 条'));
    $('overview-scope-count').replaceChildren(document.createTextNode(String(conversations.length)), element('small', '', ' 个'));
    $('collection-scope-count').replaceChildren(document.createTextNode(String(conversations.length)), element('small', '', ' 个会话'));
    $('conversation-total').textContent = conversations.length;
    $('overview-state').textContent = title;
    $('overview-state').style.color = 'var(--' + color + '-text)';
    $('nav-state-dot').style.background = 'var(--' + color + '-text)';
    $('nav-unread').textContent = String(conversations.reduce((sum, chat) => sum + chat.unread, 0));
    $('live-connection-notice').hidden = !live.error;
    $('live-connection-description').textContent = description;
    $('live-retry').disabled = live.error?.status === 401;
    $('live-retry').textContent = live.error?.status === 401 ? '请从启动器重新打开' : '重新连接';
    document.querySelector('.sidebar-local > div').replaceChildren(document.createTextNode('本机采集'), element('span', '', live.error ? '连接已中断 · 内容可能已过期' : live.connected ? title : '正在连接本机服务'));
    $('live-page-state').textContent = live.connected ? '本机服务已连接' : '尚未连接';
    $('live-resume-state').textContent = live.status?.resumed_from_checkpoint === true ? '已从保存位置继续' : live.status?.resumed_from_checkpoint === false ? '本次首次启用' : '暂无恢复记录';
    const heartbeat = liveDate(live.status?.heartbeat_at);
    $('live-heartbeat').textContent = heartbeat ? heartbeat.toLocaleTimeString('zh-CN', { hour12: false }) : '尚未收到';
    document.querySelectorAll('.property-list dd')[2].textContent = title;
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
    if (live.error?.status === 401 || epoch !== live.cycle) return;
    live.timer = setTimeout(() => live.ready ? pollLive(epoch) : connectLive(), live.failures ? Math.min(10000, 1000 * 2 ** Math.min(live.failures, 4)) : 1000);
  }

  async function connectLive() {
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
      live.session = session;
      if (location.hash) history.replaceState(null, '', location.pathname + location.search);
      const [metadata, status] = await Promise.all([api('/api/conversations'), api('/api/status')]);
      if (epoch !== live.cycle) return;
      acceptStatus(status);
      reconcileConversations(metadata);
      live.ready = true;
      live.connected = true;
      live.failures = 0;
      renderLiveStatus();
      if (activeId) await ensureLiveMessages(activeId);
    } catch (error) { if (epoch === live.cycle) showLiveFailure(error); }
    finally { scheduleLive(epoch); }
  }

  async function pollLive(epoch) {
    try {
      const [metadata, status] = await Promise.all([api('/api/conversations'), api('/api/status')]);
      if (epoch !== live.cycle) return;
      acceptStatus(status);
      reconcileConversations(metadata);
      live.connected = true;
      live.error = null;
      live.failures = 0;
      renderLiveStatus();
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
    if (!cache.loaded) { await ensureLiveMessages(id); return; }
    if (cache.loading || cache.deferred) return;
    const request = ++cache.request;
    const canFollow = () => id === activeId && currentView === 'messages' && atBottom() && preferences.autoScroll && !$('message-search').value.trim();
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
        if (!$('message-search').value.trim()) renderMessages({ bottom: follow });
        renderNewMessageButton();
      }
      renderConversations();
    } catch (error) { if (request === cache.request) throw error; }
    finally { if (request === cache.request) cache.loading = false; }
  }

  async function selectLiveConversation(id) {
    if (!conversations.some(row => row.id === id)) return;
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
    await ensureLiveMessages(id);
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

  async function openLiveOverviewQuery() {
    const query = $('overview-search').value.trim();
    setFilter('all');
    $('conversation-search').value = '';
    const match = query && conversations.find(row => row.name.toLocaleLowerCase().includes(query.toLocaleLowerCase()));
    if (match) { await selectLiveConversation(match.id); return; }
    await selectLiveConversation(activeId || conversations[0]?.id);
    navigate('messages');
    $('message-search').value = query;
    if (query) requestLiveSearch();
  }

  async function jumpLiveLatest() {
    const id = activeId;
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
    document.querySelector('.entry-label > .tag').textContent = '仅所选会话';
    document.querySelector('.entry-footer > span').textContent = '正在读取所选会话';
    document.querySelectorAll('.overview-stats > div > span')[1].textContent = '已保存文字';
    document.querySelectorAll('.overview-stats > div > span')[2].textContent = '采集状态';
    document.querySelector('.overview-note > p').replaceChildren(document.createTextNode('只阅读你选择的内容。'), element('span', '', '真实文字来自本机采集库。当前没有启用云端模型。'));
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
    document.querySelector('.technical-details p').textContent = '页面通过本机接口读取已选会话的文字与采集心跳。停止采集后内容仍可查看；重新启动会从已保存位置继续。不会读取未选会话或向云端发送聊天。';
    $('preview-dialog-title').textContent = '关注你选择的沟通。';
    const help = document.querySelectorAll('#preview-dialog > p');
    help[0].textContent = '这是本机采集工作台，只展示采集程序中已经选择的会话及启用后保存的文字消息。';
    help[1].textContent = '请保持电脑、微信与采集程序运行。页面关闭不等于停止采集；采集异常、停止或心跳过期会明确显示。当前不调用模型，也不会代发消息。';
    $('confirm-preview').textContent = '知道了';
    $('overview-search').placeholder = '输入会话名称，或在当前会话内查找文字';
    ['message-search', 'overview-search'].forEach(id => { $(id).maxLength = 256; });
    $('live-retry').addEventListener('click', connectLive);
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

  $('conversation-search').addEventListener('input', renderConversations);
  $('message-search').addEventListener('input', () => {
    if (LIVE) requestLiveSearch();
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
  $('overview-form').addEventListener('submit', event => {
    event.preventDefault();
    if (LIVE) { openLiveOverviewQuery(); return; }
    const query = $('overview-search').value.trim();
    const chat = conversations.find(row => row.name.toLocaleLowerCase().includes(query.toLocaleLowerCase()));
    setFilter('all');
    $('conversation-search').value = '';
    if (chat && query) {
      selectConversation(chat.id);
      $('message-search').value = '';
    } else {
      navigate('messages');
      $('workbench').classList.add('mobile-thread-open');
      $('message-search').value = query;
      if (query) {
        const match = conversations.find(row => row.messages.some(message => (message.body + message.sender).toLocaleLowerCase().includes(query.toLocaleLowerCase())));
        if (match) activeId = match.id;
      }
      renderHeader();
      renderConversations();
      renderMessages({ restore: 0 });
    }
    previewState('normal');
  });
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
  const settings = $('settings-dialog');
  $('settings-button').addEventListener('click', () => {
    setDrawer(false, false);
    $('pref-density').value = preferences.density;
    $('pref-autoscroll').checked = preferences.autoScroll;
    settings.showModal();
  });
  $('cancel-settings').addEventListener('click', () => settings.close());
  $('settings-form').addEventListener('submit', event => {
    event.preventDefault();
    preferences.density = $('pref-density').value;
    preferences.autoScroll = $('pref-autoscroll').checked;
    document.body.classList.toggle('compact', preferences.density === 'compact');
    settings.close();
    toast('显示偏好已应用到本页，刷新后还原。');
  });
  [dialog, settings].forEach(modal => modal.addEventListener('close', () => {
    if (window.innerWidth <= 640) $('nav-toggle').focus({ preventScroll: true });
  }));
  settings.addEventListener('click', event => { if (event.target === settings) settings.close(); });
  document.addEventListener('keydown', event => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLocaleLowerCase() === 'k' && !dialog.open && !settings.open) {
      event.preventDefault(); navigate('messages'); $('workbench').classList.remove('mobile-thread-open'); $('conversation-search').focus();
    }
    if (event.key === 'Escape' && !dialog.open && !settings.open) {
      if (document.body.classList.contains('nav-open')) setDrawer(false);
      else setDetails(false);
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
  if (LIVE) initializeLive();
  else reset();
  navigate('overview');
  setDrawer(false, false);
  if (window.innerWidth <= 1250) setDetails(false);
})();
