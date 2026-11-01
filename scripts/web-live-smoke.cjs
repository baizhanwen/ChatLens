#!/usr/bin/env node
// Real HTTP + readonly collector-store browser checks; all content is synthetic.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');
const readline = require('node:readline');
const { createHash } = require('node:crypto');
const { chromium } = require(process.env.CHATLENS_PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const args = process.argv.slice(2);
const option = (name, fallback) => args.includes(name) ? args[args.indexOf(name) + 1] : fallback;
const output = path.resolve(option('--output', path.join(root, '.build', 'web-live-smoke-' + Date.now())));
const sourceNames = ['apps/web/app.js', 'apps/web/index.html', 'apps/web/styles.css', 'scripts/windows-proof/web_data.py', 'scripts/windows-proof/web_server.py', 'tests/web_live_fixture.py', 'scripts/web-live-smoke.cjs'];
const hashes = () => Object.fromEntries(sourceNames.map(name => [name, createHash('sha256').update(fs.readFileSync(path.join(root, name))).digest('hex')]));
const before = hashes();
assert(!fs.existsSync(output), 'Choose a new evidence directory.');
fs.mkdirSync(output, { recursive: true });
const python = process.env.CHATLENS_PYTHON || path.join(root, '.build/wechat-read-venv/bin/python3.12');
const fixture = spawn(python, [path.join(root, 'tests/web_live_fixture.py')], { cwd: root, stdio: ['pipe', 'pipe', 'pipe'] });
const pending = [], backlog = [];
const lines = readline.createInterface({ input: fixture.stdout });
let stderrBytes = 0;
fixture.stderr.on('data', chunk => { stderrBytes += chunk.length; });
lines.on('line', line => {
  let value;
  try { value = JSON.parse(line); } catch { value = { error: 'fixture_invalid_response' }; }
  const next = pending.shift();
  if (next) next(value); else backlog.push(value);
});
const response = () => new Promise((resolve, reject) => {
  if (backlog.length) { resolve(backlog.shift()); return; }
  const receive = value => { clearTimeout(timeout); resolve(value); };
  const timeout = setTimeout(() => { const index = pending.indexOf(receive); if (index >= 0) pending.splice(index, 1); reject(new Error('Synthetic fixture timed out.')); }, 12000);
  pending.push(receive);
});
async function command(value) {
  fixture.stdin.write(JSON.stringify(value) + '\n');
  const result = await response();
  assert(!result.error, result.error);
  return result;
}

async function main() {
  let browser, page;
  const checks = [], issues = { page: [], external: 0 };
  let apiRequests = 0;
  const check = async (name, fn) => { await fn(); checks.push(name); };
  try {
    const ready = await response();
    assert(ready.ready, ready.error || 'Fixture failed to initialize.');
    const launch = { headless: true };
    if (process.env.CHATLENS_BROWSER_EXECUTABLE) launch.executablePath = process.env.CHATLENS_BROWSER_EXECUTABLE;
    browser = await chromium.launch(launch);
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: 'reduce' });
    await context.route('**/*', route => {
      const url = new URL(route.request().url());
      if (url.origin !== new URL(ready.launch_url).origin && url.protocol !== 'data:') { issues.external++; return route.abort(); }
      return route.continue();
    });
    context.on('request', request => { if (new URL(request.url()).pathname.startsWith('/api/')) apiRequests++; });
    page = await context.newPage();
    page.on('pageerror', () => issues.page.push('browser_runtime_error'));
    const byId = id => page.locator('#' + id);
    const rows = () => page.locator('.message-row');
    const bodyContains = text => page.waitForFunction(value => [...document.querySelectorAll('.message-body')].some(node => node.textContent === value), text);
    const navigate = async name => {
      if (page.viewportSize().width <= 640 && await byId('nav-toggle').getAttribute('aria-expanded') !== 'true') await byId('nav-toggle').click();
      await byId('nav-' + name).click();
      await byId('view-' + name).waitFor({ state: 'visible' });
    };
    const noOverflow = async () => assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
    const screenshot = name => page.screenshot({ path: path.join(output, name + '.png'), fullPage: true });
    await page.goto(ready.launch_url);
    await page.waitForFunction(() => document.querySelector('#ribbon-state').textContent === '正在采集');
    await check('authenticated_real_http_bootstrap_and_fragment_removed', async () => {
      assert.equal(new URL(page.url()).hash, '');
      assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
      const cookies = await context.cookies();
      assert(cookies.some(cookie => cookie.name === 'chatlens_session' && cookie.httpOnly && cookie.sameSite === 'Strict'));
      assert.equal(await page.evaluate(() => document.cookie.includes('chatlens_session')), false);
      assert.equal(await byId('total-messages').textContent(), '126 条');
      assert.equal(await byId('live-resume-state').innerText(), '已从保存位置继续');
      assert.notEqual(await byId('live-heartbeat').innerText(), '尚未收到');
      assert((await byId('conversation-list').textContent()).includes('合成客户·青禾'));
      assert.equal(await byId('simulate-message').isVisible(), false);
      assert.equal(await byId('reset-preview').isVisible(), false);
    });
    await screenshot('desktop-overview');
    await navigate('messages');
    await check('selected_conversations_real_latest100_and_older_pagination', async () => {
      assert.equal(await page.locator('.conversation-item').count(), 2);
      await page.waitForFunction(() => document.querySelectorAll('.message-row').length === 100);
      assert.equal(await byId('message-count').innerText(), '125 条');
      await byId('load-older').click();
      await page.waitForFunction(() => document.querySelectorAll('.message-row').length === 125);
      assert.equal(await byId('load-older').isVisible(), false);
      const ids = await rows().evaluateAll(nodes => nodes.map(node => node.dataset.messageId));
      assert.equal(new Set(ids).size, ids.length);
    });
    await check('server_search_pagination_separate_from_incremental_cursor', async () => {
      await byId('message-search').fill('合成历史文字');
      await page.waitForFunction(() => document.querySelector('#message-count').textContent === '100 条结果');
      await byId('load-older').click();
      await page.waitForFunction(() => document.querySelector('#message-count').textContent === '125 条结果');
      const top = await byId('message-scroll').evaluate(node => node.scrollTop);
      await command({ command: 'commit', body: '合成搜索期间的新消息' });
      await page.waitForFunction(() => document.querySelector('#total-messages').textContent === '127 条');
      assert.equal(await rows().count(), 125);
      assert.equal(await byId('message-scroll').evaluate(node => node.scrollTop), top);
      await byId('message-search').fill('');
      await bodyContains('合成搜索期间的新消息');
      assert.equal(await rows().count(), 126);
    });
    await check('source_commit_polling_xss_literal_unknown_sender_and_no_duplicate', async () => {
      const text = '<img src=x onerror=alert(1)> 合成中文🙂\n第二行 & <script>fixture</script>';
      await command({ command: 'commit', body: text, sender: 'unmapped-synthetic-person' });
      await bodyContains(text);
      assert.equal(await page.locator('.message-body img,.message-body script').count(), 0);
      assert((await rows().last().innerText()).includes('未知发送人'));
      await page.waitForTimeout(1600);
      const ids = await rows().evaluateAll(nodes => nodes.map(node => node.dataset.messageId));
      assert.equal(new Set(ids).size, ids.length);
    });
    await check('reading_position_preserved_and_new_message_button', async () => {
      await byId('message-scroll').evaluate(node => { node.scrollTop = 100; });
      const top = await byId('message-scroll').evaluate(node => node.scrollTop);
      await command({ command: 'commit', body: '合成保留阅读位置消息' });
      await bodyContains('合成保留阅读位置消息');
      assert(Math.abs(await byId('message-scroll').evaluate(node => node.scrollTop) - top) < 2);
      await byId('new-messages').waitFor({ state: 'visible' });
      await byId('new-messages').click();
      await page.waitForFunction(() => { const node = document.querySelector('#message-scroll'); return node.scrollHeight - node.clientHeight - node.scrollTop < 5; });
      assert.equal(await byId('new-messages').isVisible(), false);
    });
    await screenshot('desktop-messages');
    await check('conversation_switch_search_responses_cannot_replace_other_thread', async () => {
      const delayedRequest = page.waitForRequest(request => new URL(request.url()).searchParams.get('q') === '合成慢速查询');
      await byId('message-search').fill('合成慢速查询');
      await delayedRequest;
      await page.locator('[data-testid="conversation-item-group_test"]').click();
      await bodyContains('合成群聊文字');
      await page.waitForTimeout(1000);
      assert.equal(await byId('thread-title').innerText(), '合成项目测试群');
      assert.equal(await byId('message-search').inputValue(), '');
      assert.equal(await rows().count(), 1);
      await page.locator('[data-testid="conversation-item-private_test"]').click();
      await bodyContains('合成保留阅读位置消息');
    });
    await check('actual_service_stop_keeps_rows_then_reconnect_catches_up', async () => {
      await command({ command: 'offline' });
      await byId('live-connection-notice').waitFor({ state: 'visible', timeout: 15000 });
      assert((await byId('ribbon-state').innerText()).includes('连接中断'));
      assert(await rows().count() > 0);
      await command({ command: 'commit', body: '合成离线期间提交消息' });
      await command({ command: 'online' });
      await byId('live-retry').click();
      await byId('live-connection-notice').waitFor({ state: 'hidden' });
      await bodyContains('合成离线期间提交消息');
    });
    await check('actual_collector_status_does_not_claim_collecting_when_stopped', async () => {
      await command({ command: 'status', stage: 'stopped' });
      await page.waitForFunction(() => document.querySelector('#ribbon-state').textContent === '采集已停止');
      await navigate('collection');
      assert.equal(await byId('collection-stage-title').innerText(), '采集已停止');
      assert.equal(await byId('collection-toggle').isVisible(), false);
      assert((await byId('activity-log').innerText()).includes('采集已停止'));
    });
    await screenshot('desktop-collection');
    await check('390px_drawer_list_thread_detail_and_status_no_overflow', async () => {
      await page.setViewportSize({ width: 390, height: 844 });
      await noOverflow();
      await screenshot('mobile-collection');
      await navigate('messages');
      await byId('mobile-back').click();
      await noOverflow();
      await screenshot('mobile-conversations');
      await page.locator('[data-testid="conversation-item-group_test"]').click();
      await bodyContains('合成群聊文字');
      await noOverflow();
      await screenshot('mobile-thread');
      await byId('toggle-details').click();
      await byId('status-panel').waitFor({ state: 'visible' });
      await noOverflow();
      await screenshot('mobile-details');
    });
    await check('real_http_without_authorization_shows_error_and_never_fiction', async () => {
      const isolated = await browser.newContext({ viewport: { width: 390, height: 844 } });
      const emptyPage = await isolated.newPage();
      await emptyPage.goto(new URL(ready.launch_url).origin);
      await emptyPage.locator('#live-connection-notice').waitFor();
      assert.equal(await emptyPage.locator('.conversation-item,.message-row').count(), 0);
      assert((await emptyPage.locator('#live-connection-description').innerText()).includes('启动器'));
      assert.equal(await emptyPage.locator('#simulate-message').isVisible(), false);
      await isolated.close();
    });
    assert.equal(issues.page.length, 0);
    assert.equal(issues.external, 0);
    assert.deepEqual(hashes(), before, 'Source files changed during browser verification.');
    const evidence = { result: 'passed', source: 'synthetic_commits_into_actual_collector_store', transport: 'actual_authenticated_loopback_http', race_check: 'actual_db_search_response_delayed_800ms', real_wechat_validation: 'not_performed', source_sha256: before, checks, api_request_count: apiRequests, external_requests: issues.external, browser_runtime_errors: issues.page.length, fixture_stderr_bytes: stderrBytes };
    fs.writeFileSync(path.join(output, 'browser-qa.json'), JSON.stringify(evidence, null, 2) + '\n');
    process.stdout.write(JSON.stringify({ result: 'passed', checks: checks.length, evidence: path.relative(root, output) }) + '\n');
  } catch (error) {
    const state = page ? await page.evaluate(() => ({ message_count: document.querySelector('#message-count')?.textContent, rendered_rows: document.querySelectorAll('.message-row').length, state: document.querySelector('#ribbon-state')?.textContent, notice: document.querySelector('#live-connection-description')?.textContent })).catch(() => ({})) : {};
    if (page) await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {});
    fs.writeFileSync(path.join(output, 'failure.json'), JSON.stringify({ result: 'failed', completed_checks: checks, state, reason: String(error.message).replace(/http:\/\/127\.0\.0\.1:[0-9]+[^ ]*/g, '[local-url]') }, null, 2) + '\n');
    process.stdout.write(JSON.stringify({ result: 'failed', completed_checks: checks.length, evidence: path.relative(root, output), reason: error.message.split('\n')[0] }) + '\n');
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
    if (fixture.exitCode === null) {
      try { await command({ command: 'exit' }); } catch { /* final kill still releases temporary resources */ }
      fixture.stdin.end();
      const kill = setTimeout(() => fixture.kill('SIGTERM'), 2000);
      await new Promise(resolve => { if (fixture.exitCode !== null) resolve(); else fixture.once('exit', resolve); });
      clearTimeout(kill);
    }
    lines.close();
  }
}
main();
