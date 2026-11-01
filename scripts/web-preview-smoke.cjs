#!/usr/bin/env node
// Real browser checks for the standalone synthetic frontend. No collector/API access.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { createHash } = require('node:crypto');

const { chromium } = require(process.env.CHATLENS_PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const args = process.argv.slice(2);
function argument(name, fallback) {
  const index = args.indexOf(name);
  return index < 0 ? fallback : args[index + 1];
}
const target = argument('--url', pathToFileURL(path.join(root, 'apps/web/index.html')).href);
const output = path.resolve(argument('--output', path.join(root, '.build/web-preview-smoke')));
assert(!fs.existsSync(output), 'Use a new output directory to preserve prior evidence.');
fs.mkdirSync(output, { recursive: true });
const sourceFiles = ['apps/web/index.html', 'apps/web/app.js', 'apps/web/styles.css', 'apps/web/assets/chatlens-mark.svg'];
function sourceHashes() {
  return Object.fromEntries(sourceFiles.map(name => [name,
    createHash('sha256').update(fs.readFileSync(path.join(root, name))).digest('hex')]));
}
const sourceBefore = sourceHashes();

async function main() {
  const options = { headless: true };
  if (process.env.CHATLENS_BROWSER_EXECUTABLE) options.executablePath = process.env.CHATLENS_BROWSER_EXECUTABLE;
  const browser = await chromium.launch(options);
  const checks = [];
  const issues = { pageErrors: [], consoleErrors: [], externalRequests: [], apiRequests: [] };
  const requests = [];
  const allowed = (url) => {
    const parsed = new URL(url);
    return parsed.protocol === 'file:' || parsed.protocol === 'data:' ||
      (['http:', 'https:'].includes(parsed.protocol) && ['127.0.0.1', 'localhost'].includes(parsed.hostname));
  };
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: 'reduce' });
  await context.route('**/*', route => {
    const url = route.request().url();
    if (!allowed(url)) { issues.externalRequests.push(url); return route.abort(); }
    return route.continue();
  });
  context.on('request', request => {
    const url = new URL(request.url());
    requests.push({ protocol: url.protocol, type: request.resourceType() });
    if (/\/api(?:\/|$)|websocket/i.test(url.pathname)) issues.apiRequests.push(url.pathname);
  });
  const page = await context.newPage();
  page.on('pageerror', error => issues.pageErrors.push(error.message));
  page.on('console', message => { if (message.type() === 'error') issues.consoleErrors.push(message.text()); });
  async function check(name, fn) { await fn(); checks.push(name); }
  const conversations = () => page.locator('[data-testid^="conversation-item-"]');
  const scroll = () => page.getByTestId('message-scroll');
  async function screenshot(name) { await page.screenshot({ path: path.join(output, name), fullPage: true }); }
  async function horizontalOverflow() {
    return page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
  }
  try {
    await page.goto(target);
    await conversations().first().waitFor();
    await check('visible_brand_and_synthetic_preview_boundary', async () => {
      assert(await page.getByText('利明智耀', { exact: true }).isVisible());
      assert(await page.getByText('前端预览 · 虚构数据', { exact: true }).isVisible());
      assert((await page.getByTestId('status-panel').innerText()).includes('尚未接入'));
      assert((await page.locator('#collection-ribbon').innerText()).includes('未连接真实微信'));
    });
    await check('desktop_has_three_focus_conversations_and_no_horizontal_overflow', async () => {
      assert.equal(await conversations().count(), 3);
      assert.equal(await horizontalOverflow(), false);
    });
    await screenshot('desktop.png');
    await check('conversation_switch_changes_thread', async () => {
      const first = await page.locator('#thread-title').innerText();
      await conversations().nth(1).click();
      assert.notEqual(await page.locator('#thread-title').innerText(), first);
      await conversations().first().click();
    });
    await check('conversation_search_and_empty_result', async () => {
      const search = page.getByTestId('conversation-search');
      await search.fill('SYNTHETIC_NO_MATCH_73A');
      assert.equal(await conversations().count(), 0);
      assert(await page.locator('#conversation-list .empty-state').isVisible());
      await search.fill('');
      assert.equal(await conversations().count(), 3);
    });
    await check('message_search_and_empty_result', async () => {
      const search = page.getByTestId('message-search');
      const body = await page.locator('.message-body').first().innerText();
      await search.fill(body.slice(0, Math.min(4, body.length)));
      assert((await page.locator('.message-body').count()) > 0);
      await search.fill('SYNTHETIC_NO_MESSAGE_73A');
      assert(await scroll().locator('.empty-state').isVisible());
      assert.equal(await page.locator('.message-body').count(), 0);
      await search.fill('');
      assert((await page.locator('.message-body').count()) > 0);
    });
    await check('new_message_during_search_has_explicit_way_back_to_latest', async () => {
      await page.getByTestId('message-search').fill('SYNTHETIC_FILTER_NEW_MESSAGE_73A');
      await page.getByTestId('simulate-message').click();
      assert(await page.getByTestId('new-messages').isVisible());
      await page.getByTestId('new-messages').click();
      assert.equal(await page.getByTestId('message-search').inputValue(), '');
      assert((await page.locator('.message-body').count()) > 0);
    });
    await check('new_messages_preserve_reader_scroll_then_scroll_to_latest', async () => {
      for (let i = 0; i < 3; i++) await page.getByTestId('simulate-message').click();
      const dimensions = await scroll().evaluate(node => ({ height: node.clientHeight, content: node.scrollHeight }));
      assert(dimensions.content > dimensions.height + 40, 'Synthetic thread must overflow for scroll behavior check.');
      await scroll().evaluate(node => { node.scrollTop = 0; });
      await page.waitForTimeout(50);
      const top = await scroll().evaluate(node => node.scrollTop);
      await page.getByTestId('simulate-message').click();
      await page.waitForTimeout(50);
      assert(Math.abs((await scroll().evaluate(node => node.scrollTop)) - top) <= 2);
      assert(await page.getByTestId('new-messages').isVisible());
      await screenshot('desktop-new-message.png');
      await page.getByTestId('new-messages').click();
      await page.waitForFunction(() => {
        const node = document.querySelector('[data-testid="message-scroll"]');
        return node.scrollHeight - node.clientHeight - node.scrollTop <= 4;
      });
      const remaining = await scroll().evaluate(node => node.scrollHeight - node.clientHeight - node.scrollTop);
      assert(remaining <= 4, 'New-message button should reach latest message.');
    });
    await check('paused_demo_blocks_simulation_and_keyboard_resume_works', async () => {
      const toggle = page.getByTestId('toggle-collection');
      await toggle.click();
      assert(await page.getByTestId('simulate-message').isDisabled());
      assert.match(await page.locator('#status-title').innerText(), /暂停/);
      await screenshot('desktop-paused.png');
      await toggle.focus();
      await page.keyboard.press('Enter');
      assert(await page.getByTestId('simulate-message').isEnabled());
    });
    await check('connection_interruption_and_restore_are_demo_only', async () => {
      await page.getByTestId('connection-demo').click();
      assert(await page.getByTestId('simulate-message').isDisabled());
      assert.match(await page.locator('#status-title').innerText(), /中断|断开/);
      assert((await page.getByTestId('status-panel').innerText()).includes('尚未接入'));
      await screenshot('desktop-disconnected.png');
      await page.getByTestId('connection-demo').click();
      assert(await page.getByTestId('simulate-message').isEnabled());
    });
    await check('preview_dialog_can_open_and_close_with_escape', async () => {
      await page.locator('#preview-help').click();
      assert(await page.getByRole('dialog').isVisible());
      await page.keyboard.press('Escape');
      assert.equal(await page.getByRole('dialog').isVisible(), false);
    });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.reload();
    await conversations().first().waitFor();
    await check('mobile_conversation_view_and_brand_fit', async () => {
      assert(await page.getByText('利明智耀', { exact: true }).isVisible());
      assert(await page.getByText('前端预览 · 虚构数据', { exact: true }).isVisible());
      assert.equal(await horizontalOverflow(), false);
      await screenshot('mobile-conversations.png');
    });
    await check('mobile_thread_status_and_back_navigation', async () => {
      await conversations().first().click();
      assert(await scroll().isVisible());
      const initialRemaining = await scroll().evaluate(node => node.scrollHeight - node.clientHeight - node.scrollTop);
      assert(initialRemaining <= 4, 'First opening of a mobile thread should show its latest messages.');
      assert.equal(await horizontalOverflow(), false);
      await screenshot('mobile-thread.png');
      await page.locator('#toggle-details').click();
      assert(await page.getByTestId('status-panel').isVisible());
      await screenshot('mobile-status.png');
      await page.locator('#close-details').click();
      assert.equal(await page.getByTestId('status-panel').isVisible(), false);
      await page.locator('#mobile-back').click();
      assert(await conversations().first().isVisible());
      assert.equal(await horizontalOverflow(), false);
    });
    await check('no_backend_external_requests_or_browser_errors', async () => {
      for (const values of Object.values(issues)) assert.equal(values.length, 0);
      assert(requests.every(request => ['file:', 'data:', 'http:', 'https:'].includes(request.protocol)));
      assert.equal(requests.filter(request => ['fetch', 'xhr', 'websocket', 'eventsource'].includes(request.type)).length, 0);
      assert.deepEqual(sourceHashes(), sourceBefore, 'Source changed while verifying; use a new output directory after it settles.');
    });
    const result = { schema_version: 1, status: '通过', scope: 'frontend_only_synthetic_browser_qa',
      recorded_at_utc: new Date().toISOString(), entry_protocol: new URL(target).protocol,
      browser: 'Chromium', browser_version: browser.version(), viewports: ['1440x1000', '390x844'],
      passed_checks: checks, passed_check_count: checks.length, page_errors: issues.pageErrors.length,
      console_errors: issues.consoleErrors.length, external_requests: issues.externalRequests.length,
      api_requests: issues.apiRequests.length, total_static_requests: requests.length,
      source_files_sha256: sourceBefore,
      real_wechat_connected: false, backend_implemented: false, real_data_read: false,
      boundary: 'Interactive frontend preview using explicitly synthetic in-memory data; no backend, real chat, collector integration or mobile-device validation.' };
    fs.writeFileSync(path.join(output, 'browser-qa.json'), JSON.stringify(result, null, 2) + '\n', { flag: 'wx' });
    console.log(JSON.stringify(result, null, 2));
  } catch (error) {
    await screenshot('failure.png').catch(() => {});
    fs.writeFileSync(path.join(output, 'browser-qa-failure.json'), JSON.stringify({ schema_version: 1,
      status: '失败', completed_checks: checks, page_error_count: issues.pageErrors.length,
      console_error_count: issues.consoleErrors.length, external_request_count: issues.externalRequests.length,
      api_request_count: issues.apiRequests.length }, null, 2) + '\n', { flag: 'wx' });
    throw error;
  } finally { await browser.close(); }
}
main().catch(error => { console.error(error.stack); process.exitCode = 1; });
