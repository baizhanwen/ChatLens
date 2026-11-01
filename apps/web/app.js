/* ChatLens frontend preview: fictional data in page memory, no network or storage. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const svgNS = 'http://www.w3.org/2000/svg';
  const initialDate = new Date();
  let sequence = 100;
  let conversations;
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

  function activeChat() { return conversations.find((row) => row.id === activeId); }
  function timeText(date) { return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false }); }
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
      const last = chat.messages[chat.messages.length - 1];
      const button = element('button', 'conversation-item' + (chat.id === activeId ? ' selected' : ''));
      button.type = 'button';
      button.setAttribute('data-testid', 'conversation-item-' + chat.id);
      button.setAttribute('aria-label', chat.name + (chat.unread ? '，' + chat.unread + ' 条演示未读消息' : ''));
      button.setAttribute('aria-pressed', String(chat.id === activeId));
      const avatar = element('span', 'avatar ' + chat.color, chat.short);
      avatar.setAttribute('aria-hidden', 'true');
      const copy = element('span', 'conversation-copy');
      const title = element('span', 'conversation-title-row');
      title.append(element('span', 'conversation-name', chat.name), element('time', 'conversation-time', timeText(last.date)));
      const preview = element('span', 'conversation-preview-row');
      const snippet = (chat.type === 'group' ? last.sender + '：' : '') + last.body.replace(/\n/g, ' ');
      preview.append(element('span', 'conversation-preview', snippet));
      if (chat.unread) preview.append(element('span', 'unread-count', chat.unread > 99 ? '99+' : String(chat.unread)));
      copy.append(title, preview);
      button.append(avatar, copy);
      button.addEventListener('click', () => selectConversation(chat.id));
      list.append(button);
    }
    if (!filtered.length) list.append(emptyState('没有找到会话', '试试其他名称，或切换会话类型。'));
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
    const messages = chat.messages.filter((message) => !query || (message.body + ' ' + message.sender).toLocaleLowerCase().includes(lowered));
    const list = $('message-list');
    list.replaceChildren();
    let previousDay = '';
    for (const message of messages) {
      const key = message.date.toDateString();
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
      timestamp.dateTime = message.date.toISOString();
      meta.append(element('span', 'message-sender', message.sender), timestamp);
      const body = element('p', 'message-body');
      appendHighlighted(body, message.body, query);
      content.append(meta, body);
      row.append(avatar, content);
      list.append(row);
    }
    $('message-count').textContent = query ? messages.length + ' 条结果' : chat.messages.length + ' 条';
    if (!messages.length) list.append(emptyState('没有找到相关消息', '换个关键词试试。搜索仅限当前虚构会话。'));
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
    $('thread-subtitle').textContent = chat.subtitle + ' · 仅演示所选新文字';
  }

  function selectConversation(id) {
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
    const sorted = [...conversations].sort((a, b) => b.messages.at(-1).date - a.messages.at(-1).date);
    for (const chat of sorted) {
      const last = chat.messages.at(-1);
      const row = element('button', 'recent-row');
      row.type = 'button';
      row.setAttribute('aria-label', '打开' + chat.name);
      const copy = element('span', 'recent-copy');
      copy.append(element('strong', '', chat.name), element('span', '', last.body.replace(/\n/g, ' ')));
      row.append(element('span', 'avatar ' + chat.color, chat.short), copy, element('time', '', timeText(last.date)), icon('chevron'));
      row.addEventListener('click', () => selectConversation(chat.id));
      list.append(row);
    }
  }

  function addLog(kind) {
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
    if (collectionState === 'disconnected') return;
    collectionState = collectionState === 'running' ? 'paused' : 'running';
    addLog(collectionState === 'paused' ? 'paused' : 'resumed');
    renderStatus();
    toast(collectionState === 'paused' ? '已暂停本页演示接收。' : '已恢复本页演示接收。');
  }

  function toggleConnection() {
    collectionState = collectionState === 'disconnected' ? 'running' : 'disconnected';
    addLog(collectionState === 'disconnected' ? 'disconnected' : 'reconnected');
    renderStatus();
    toast(collectionState === 'disconnected' ? '正在演示连接中断，真实服务尚未接入。' : '演示连接已恢复。');
  }

  $('conversation-search').addEventListener('input', renderConversations);
  $('message-search').addEventListener('input', () => renderMessages({ restore: 0 }));
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
    const seconds = Math.floor((Date.now() - demoStartedAt) / 1000);
    $('demo-elapsed').textContent = String(Math.floor(seconds / 60)).padStart(2, '0') + ':' + String(seconds % 60).padStart(2, '0');
  }, 1000);
  $('search-shortcut').textContent = /Mac|iPhone|iPad/.test(navigator.platform) ? '⌘ K' : 'Ctrl K';
  reset();
  navigate('overview');
  setDrawer(false, false);
  if (window.innerWidth <= 1250) setDetails(false);
})();
