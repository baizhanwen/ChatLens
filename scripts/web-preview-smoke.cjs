#!/usr/bin/env node
// Actual browser acceptance of the frontend-only synthetic ChatLens workspace.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { createHash } = require('node:crypto');
const { chromium } = require(process.env.CHATLENS_PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const args = process.argv.slice(2);
const option = (name, fallback) => args.includes(name) ? args[args.indexOf(name) + 1] : fallback;
const target = option('--url', pathToFileURL(path.join(root, 'apps/web/index.html')).href);
const output = path.resolve(option('--output', path.join(root, '.build', 'web-preview-settings-' + Date.now())));
assert(!fs.existsSync(output), 'Use a new evidence directory; previous results are preserved.');
fs.mkdirSync(output, { recursive: true });
const sources = ['apps/web/index.html', 'apps/web/app.js', 'apps/web/styles.css', 'apps/web/assets/chatlens-mark.svg', 'scripts/web-preview-smoke.cjs'];
const hashes = () => Object.fromEntries(sources.map(name => [name, createHash('sha256').update(fs.readFileSync(path.join(root, name))).digest('hex')]));
const before = hashes();
const colors = ['#FAFBF9','#F3F5F0','#FCFDFB','#FFFFFF','#242C29','#78856D','#53634A','#5F7054','#E5E9E3','#29322A','#FAFBF6','#40513B','#E3EADB','#34492C','#E9EDE4','#E9F0DF'];

async function main() {
  const launch = { headless: true };
  if (process.env.CHATLENS_BROWSER_EXECUTABLE) launch.executablePath = process.env.CHATLENS_BROWSER_EXECUTABLE;
  const browser = await chromium.launch(launch);
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: 'reduce' });
  const issues = { page: [], console: [], external: [], api: [] }, requests = [], checks = [];
  await context.route('**/*', route => {
    const url = new URL(route.request().url());
    const allowed = ['file:', 'data:'].includes(url.protocol) || (['http:', 'https:'].includes(url.protocol) && ['127.0.0.1','localhost'].includes(url.hostname));
    if (!allowed) { issues.external.push(url.protocol); return route.abort(); }
    return route.continue();
  });
  context.on('request', request => {
    const url = new URL(request.url()); requests.push({ protocol: url.protocol, type: request.resourceType() });
    if (/\/api(?:\/|$)|websocket/i.test(url.pathname)) issues.api.push(request.resourceType());
  });
  const page = await context.newPage();
  page.on('pageerror', e => issues.page.push(e.message));
  page.on('console', m => { if (m.type() === 'error') issues.console.push(m.text()); });
  const byId = id => page.locator('#' + id);
  const byTest = id => page.getByTestId(id);
  const section = name => byId('view-' + name);
  const rows = () => page.locator('[data-testid^="conversation-item-"]');
  const scroll = () => byTest('message-scroll');
  async function check(name, fn) { await fn(); checks.push(name); }
  const shot = name => page.screenshot({ path: path.join(output, name), fullPage: !name.includes('drawer') });
  async function noOverflow() { assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false); }
  async function visibleText(text) {
    for (const item of await page.getByText(text, { exact: true }).all()) if (await item.isVisible()) return true;
    return false;
  }
  async function navigate(name) {
    if (page.viewportSize().width <= 640 && await byId('nav-toggle').getAttribute('aria-expanded') !== 'true') await byId('nav-toggle').click();
    await byId(name === 'settings' ? 'settings-button' : 'nav-' + name).click(); await section(name).waitFor({ state: 'visible' });
  }
  async function state(value) { await navigate('messages'); await byId('preview-state').selectOption(value); }
  async function bottom() {
    await page.waitForFunction(() => { const n = document.querySelector('[data-testid="message-scroll"]'); return n && n.scrollHeight - n.clientHeight - n.scrollTop <= 4; });
  }
  try {
    await page.goto(target); await section('overview').waitFor();
    await check('shared_exact_color_tokens_and_font_stack', async () => {
      const values = await page.evaluate(() => { const s = getComputedStyle(document.documentElement); return { colors: Array.from(s).filter(n => n.startsWith('--')).map(n => s.getPropertyValue(n).trim().toUpperCase()), font: getComputedStyle(document.body).fontFamily }; });
      for (const color of colors) assert(values.colors.includes(color), 'Required design token missing: ' + color);
      assert(/Inter/.test(values.font) && /PingFang SC/.test(values.font) && /Microsoft YaHei/.test(values.font));
    });
    await check('desktop_238px_sidebar_76px_topbar_and_visible_brand', async () => {
      assert(await visibleText('利明智耀')); assert(await visibleText('前端预览 · 虚构数据'));
      assert.equal(Math.round((await byTest('app-sidebar').boundingBox()).width), 238);
      assert.equal(Math.round((await byTest('topbar').boundingBox()).height), 76);
      assert.equal(await byId('nav-toggle').isVisible(), false); assert.equal(await byId('close-nav').isVisible(), false);
      assert.equal(await byTest('app-sidebar').evaluate(n => getComputedStyle(n).backgroundColor), 'rgb(243, 245, 240)'); await noOverflow();
    });
    await shot('desktop-overview.png');
    await check('ai_preview_prompt_and_explicit_fictional_answer_open_original_evidence', async () => {
      await page.locator('[data-ai-prompt="agreement"]').click();
      assert((await byId('ai-question').inputValue()).includes('约定'));
      await byId('ai-send').click();
      await page.waitForFunction(() => document.querySelectorAll('.ai-inline-citation').length === 2);
      assert((await byId('ai-turns').innerText()).includes('虚构回答示例'));
      await page.locator('.ai-inline-citation').first().click();
      assert(await section('messages').isVisible()); assert((await byId('thread-title').innerText()).includes('林'));
    });
    await check('four_shared_navigation_views_preserve_context', async () => {
      for (const name of ['collection','settings','overview','messages']) { await navigate(name); await noOverflow(); }
      assert((await byTest('status-panel').innerText()).includes('尚未接入'));
    });
    await byTest('conversation-search').fill('');
    await check('conversation_type_filter_and_row_selection', async () => {
      const unreadBefore = Number((await byId('nav-unread').textContent()).replace(/\D/g,''));
      assert.equal(await rows().count(), 3); await page.locator('[data-filter="group"]').click(); assert.equal(await rows().count(), 1);
      await rows().first().click(); assert((await byId('thread-title').innerText()).includes('群'));
      assert(Number((await byId('nav-unread').textContent()).replace(/\D/g,'')) < unreadBefore, 'Opening unread group should update shared navigation badge.');
      await page.locator('[data-filter="private"]').click(); assert.equal(await rows().count(), 2);
      await page.locator('[data-filter="all"]').click(); await rows().first().click(); assert.equal(await rows().count(), 3);
    });
    await shot('desktop-messages.png');
    await check('ai_evidence_jump_resets_type_filter_to_reveal_source_contact', async () => {
      await page.locator('[data-filter="group"]').click(); await navigate('overview');
      await page.locator('.ai-inline-citation').first().click();
      assert(await section('messages').isVisible()); assert((await byId('thread-title').innerText()).includes('林'));
      assert((await rows().allTextContents()).some(text => text.includes('林')));
      assert.equal(await page.locator('[data-filter="all"]').getAttribute('aria-pressed'), 'true');
      await byTest('conversation-search').fill('');
    });
    await check('focused_search_and_empty_conversation_feedback', async () => {
      const input = byTest('conversation-search');
      assert((await byId('preview-state').boundingBox()).height >= 40);
      assert(await byTest('message-search').evaluate(n => (n.closest('.input-field') || n).getBoundingClientRect().height >= 40));
      await input.focus();
      const focused = await input.evaluate(n => { const nodes = [n,n.parentElement,n.parentElement?.parentElement].filter(Boolean); return nodes.map(x => getComputedStyle(x).borderColor); });
      assert(focused.includes('rgb(153, 170, 132)'), 'Focused shared field should use specified sage border.');
      await input.fill('SYNTHETIC_NO_CONVERSATION_73A'); assert.equal(await rows().count(), 0); assert(await byId('conversation-list').locator('.empty-state').isVisible());
      await input.fill(''); assert.equal(await rows().count(), 3);
    });
    await check('message_search_highlight_and_empty_feedback', async () => {
      const input = byTest('message-search'), text = await page.locator('.message-body').first().innerText();
      await input.fill(text.slice(0,Math.min(4,text.length))); assert((await page.locator('.message-body mark').count()) > 0);
      await input.fill('SYNTHETIC_NO_MESSAGE_73A'); assert(await scroll().locator('.empty-state').isVisible()); assert.equal(await page.locator('.message-body').count(), 0); await input.fill('');
    });
    await check('new_message_during_search_offers_explicit_jump_to_latest', async () => {
      await byTest('message-search').fill('SYNTHETIC_FILTER_NEW_73A'); await byTest('simulate-message').click(); assert(await byTest('new-messages').isVisible());
      await byTest('new-messages').click(); assert.equal(await byTest('message-search').inputValue(), ''); await bottom();
    });
    await check('arrival_preserves_reading_position_then_jump_reaches_latest', async () => {
      for (let n = 0; n < 3; n++) await byTest('simulate-message').click();
      await scroll().evaluate(n => { n.scrollTop = 0; }); await page.waitForTimeout(50); const previous = await scroll().evaluate(n => n.scrollTop);
      await byTest('simulate-message').click(); assert(Math.abs((await scroll().evaluate(n => n.scrollTop)) - previous) <= 2); assert(await byTest('new-messages').isVisible());
      await shot('desktop-new-message.png'); await byTest('new-messages').click(); await bottom();
    });
    await check('leaving_message_view_preserves_old_reading_position', async () => {
      await scroll().evaluate(n => { n.scrollTop = 35; }); const position = await scroll().evaluate(n => n.scrollTop);
      await navigate('overview'); await navigate('messages');
      assert(Math.abs((await scroll().evaluate(n => n.scrollTop)) - position) <= 2);
    });
    await check('pause_disables_arrival_and_keyboard_resume_restores_it', async () => {
      await byTest('toggle-collection').click(); assert(await byTest('simulate-message').isDisabled()); assert.match(await byId('status-title').innerText(), /暂停/);
      await byTest('toggle-collection').focus(); await page.keyboard.press('Enter'); assert(await byTest('simulate-message').isEnabled());
    });
    await check('disconnect_and_reconnect_stay_explicitly_synthetic', async () => {
      await byTest('connection-demo').click(); assert(await byTest('simulate-message').isDisabled()); assert.match(await byId('status-title').innerText(), /中断|断开/);
      assert((await byTest('status-panel').innerText()).includes('尚未接入')); await shot('desktop-disconnected.png'); await byTest('connection-demo').click(); assert(await byTest('simulate-message').isEnabled());
    });
    await check('collection_page_stage_counts_and_expandable_details', async () => {
      await navigate('collection'); assert.match(await section('collection').innerText(), /演示/);
      const details = section('collection').locator('details').first(); assert(await details.count()); if (!await details.evaluate(n => n.open)) await details.locator('summary').click(); assert(await details.evaluate(n => n.open));
      await byId('collection-toggle').click(); assert.match(await section('collection').innerText(), /暂停/); await byId('collection-toggle').click(); await shot('desktop-collection.png');
    });
    await check('dedicated_settings_page_keeps_display_preferences_in_memory', async () => {
      await navigate('settings'); assert(await section('settings').isVisible()); assert(await byId('model-form').isVisible()); assert(await byId('settings-form').isVisible()); assert.equal(await page.locator('#model-dialog,#settings-dialog').count(), 0);
      await byId('pref-density').selectOption('compact'); await byId('pref-autoscroll').uncheck(); await byId('save-settings').click(); assert(await section('settings').isVisible());
      await navigate('overview'); assert.equal(await byId('ai-model-target').evaluate(node => node.tagName), 'SPAN'); await navigate('settings'); assert.equal(await byId('pref-density').inputValue(), 'compact'); assert.equal(await byId('pref-autoscroll').isChecked(), false); await noOverflow(); await shot('desktop-settings.png');
      await byId('pref-density').selectOption('spacious'); await byId('pref-autoscroll').check(); await byId('save-settings').click();
    });
    await check('local_loading_empty_error_and_retry_states', async () => {
      await state('loading'); assert(await byId('retry-preview').isVisible()); assert(await byId('retry-preview').isDisabled());
      assert.equal(await byId('retry-preview').getAttribute('aria-busy'), 'true'); assert(await byId('retry-preview').evaluate(n => n.classList.contains('is-loading')));
      await shot('desktop-loading.png'); await page.waitForFunction(() => document.querySelector('#preview-state')?.value === 'normal');
      assert.equal(await byId('retry-preview').getAttribute('aria-busy'), 'false'); assert.equal(await byId('retry-preview').isVisible(), false);
      await state('empty'); assert.match(await section('messages').innerText(), /暂无|没有|空/); await shot('desktop-empty.png');
      await state('error'); assert(await byId('retry-preview').isVisible()); assert(await byId('retry-preview').isEnabled()); assert.equal(await byId('retry-preview').getAttribute('aria-busy'), 'false'); await shot('desktop-error.png'); await byId('retry-preview').click();
      await page.waitForFunction(() => document.querySelector('#preview-state')?.value === 'normal');
    });
    await check('preview_explanation_modal_closes_with_escape', async () => {
      await byId('preview-help').click(); assert(await byId('preview-dialog').isVisible()); await page.keyboard.press('Escape'); assert.equal(await byId('preview-dialog').isVisible(), false);
    });
    await check('988px_screen_all_views_and_settings_have_no_horizontal_overflow', async () => {
      await page.setViewportSize({ width: 988, height: 776 }); for (const name of ['overview','messages','collection','settings']) { await navigate(name); await noOverflow(); }
      await shot('desktop-settings-988.png'); await navigate('overview'); await shot('desktop-overview-988.png');
    });
    await page.setViewportSize({ width: 390, height: 844 }); await page.reload(); await section('overview').waitFor();
    await check('mobile_62px_topbar_brand_and_overview_fit', async () => {
      assert.equal(Math.round((await byTest('topbar').boundingBox()).height), 62); assert(await visibleText('利明智耀')); assert(await visibleText('前端预览 · 虚构数据')); await noOverflow(); await shot('mobile-overview.png');
    });
    await check('mobile_245px_drawer_escape_overlay_and_focus_return', async () => {
      const toggle = byId('nav-toggle'); await toggle.click(); assert(await byTest('app-sidebar').isVisible()); assert.equal(Math.round((await byTest('app-sidebar').boundingBox()).width), 245); await shot('mobile-drawer.png'); assert.equal(await toggle.getAttribute('aria-expanded'), 'true');
      const bounds = await byTest('app-sidebar').boundingBox(); assert(Math.abs(bounds.x) <= 1 && Math.abs(bounds.x + bounds.width - 245) <= 1); assert(await byId('sidebar-backdrop').isVisible());
      await page.keyboard.press('Escape'); assert.equal(await toggle.getAttribute('aria-expanded'), 'false'); assert(await toggle.evaluate(n => n === document.activeElement));
      await toggle.click(); await byId('sidebar-backdrop').click({ position: { x: 370, y: 400 } }); assert.equal(await toggle.getAttribute('aria-expanded'), 'false');
    });
    await check('mobile_conversation_selection_opens_latest_messages', async () => {
      await navigate('messages'); await rows().first().waitFor(); await noOverflow(); await shot('mobile-conversations.png');
      await rows().first().click(); assert(await scroll().isVisible()); await bottom(); await noOverflow(); assert(await byTest('message-search').evaluate(n => (n.closest('.input-field') || n).getBoundingClientRect().height >= 40)); await shot('mobile-thread.png');
    });
    await check('mobile_details_back_and_collection_navigation', async () => {
      await byId('toggle-details').click(); assert(await byTest('status-panel').isVisible()); await shot('mobile-details.png'); await byId('close-details').click();
      await byId('mobile-back').click(); assert(await rows().first().isVisible()); await navigate('collection'); await noOverflow(); await shot('mobile-collection.png');
    });
    await check('mobile_settings_page_navigation_and_help_close_restore_drawer_focus', async () => {
      await navigate('settings'); assert(await section('settings').isVisible()); assert(await byId('model-form').isVisible()); assert(await byId('settings-form').isVisible()); assert.equal(await byId('nav-toggle').getAttribute('aria-expanded'), 'false'); await noOverflow(); await shot('mobile-settings.png');
      await page.keyboard.press('Escape'); assert(await section('settings').isVisible()); await navigate('overview');
      await byId('nav-toggle').click(); await byId('preview-help').click(); assert(await byId('preview-dialog').isVisible());
      await page.keyboard.press('Escape'); await page.waitForFunction(() => document.activeElement === document.querySelector('#nav-toggle'), null, { timeout: 1500 });
    });
    await check('no_external_api_requests_or_browser_errors_and_frozen_sources', async () => {
      for (const values of Object.values(issues)) assert.equal(values.length, 0); assert.equal(requests.filter(x => ['fetch','xhr','websocket','eventsource'].includes(x.type)).length, 0); assert.deepEqual(hashes(), before);
    });
    const result = { schema_version: 1, status: '通过', scope: 'frontend_only_unified_design_system_browser_qa', recorded_at_utc: new Date().toISOString(), entry_protocol: new URL(target).protocol,
      browser: 'Chromium', browser_version: browser.version(), viewports: ['1440x1000','988x776','390x844'], settings_surface: 'dedicated_page', passed_checks: checks, passed_check_count: checks.length,
      required_shared_color_tokens: colors, page_errors: issues.page.length, console_errors: issues.console.length, external_requests: issues.external.length, api_requests: issues.api.length,
      total_static_requests: requests.length, source_files_sha256: before, real_wechat_connected: false, backend_contacted: false, real_data_read: false,
      boundary: 'In-memory fictional frontend only. Desktop/narrow Chromium viewports do not verify physical phones, other operating systems or real collection.' };
    fs.writeFileSync(path.join(output,'browser-qa.json'),JSON.stringify(result,null,2)+'\n',{flag:'wx'}); console.log(JSON.stringify(result,null,2));
  } catch (error) {
    await shot('failure.png').catch(() => {}); fs.writeFileSync(path.join(output,'browser-qa-failure.json'),JSON.stringify({schema_version:1,status:'失败',completed_checks:checks,page_error_count:issues.page.length,console_error_count:issues.console.length,external_request_count:issues.external.length,api_request_count:issues.api.length},null,2)+'\n',{flag:'wx'}); throw error;
  } finally { await browser.close(); }
}
main().catch(e => { console.error(e.stack); process.exitCode=1; });
