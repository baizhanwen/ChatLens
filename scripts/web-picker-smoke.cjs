#!/usr/bin/env node
'use strict';
// Explicit synthetic HTTP contracts for the production picker state machine.
// These responses do not execute the Windows manager, native APIs, or WeChat.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {createHash} = require('node:crypto');
const {chromium} = require(process.env.CHATLENS_PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..'), args = process.argv.slice(2), at = args.indexOf('--output');
const output = path.resolve(at < 0 ? path.join(root, '.build', 'picker-' + Date.now()) : args[at + 1]);
assert(!fs.existsSync(output), 'Choose a new evidence directory.');
fs.mkdirSync(output, {recursive:true});
const sources = ['apps/web/connect.js', 'apps/web/connect.html', 'apps/web/connect.css', 'apps/web/styles.css', 'scripts/web-picker-smoke.cjs'];
const hashes = () => Object.fromEntries(sources.map(name => [name, createHash('sha256').update(fs.readFileSync(path.join(root, name))).digest('hex')]));
const before = hashes(), checks = [], counts = {catalog:0, conversations:0, setup:0}, issues = {runtime:0, external:0};
const generationA = 'synthetic_generation_A_20261101', generationB = 'synthetic_generation_B_20261101';
let generation = generationA, catalog, postMode = 'loading', listMode = 'ready', held = null, heldReady = null, unsafeDiagnostics = false, scenario = 0;
const rows = () => generation === generationA ? [{id:'choice_private', name:'合成客户', type:'private', selected:true}, {id:'choice_group', name:'合成测试群', type:'group', selected:true}] : [{id:'choice_new', name:'新范围合成客户', type:'private', selected:false}];
const makeCatalog = (state, extra = {}) => ({schema_version:1, state, phase:state === 'ready' ? 'complete' : state === 'error' ? 'failed' : state === 'loading' ? 'reading_contacts' : 'idle', reason:state === 'error' ? 'windows_scope_catalog_timeout' : null, elapsed_seconds:state === 'loading' ? 3 : 0, worker_active:state === 'loading', databases_checked:state === 'loading' ? 2 : 0, tables_seen:0, contacts_seen:state === 'loading' ? 28 : 0, conversations_found:0, ...extra});
const setup = () => ({platform:'windows', stage:'running', reason:catalog.reason, account:{detected:true, display_name:null}, checks:{wechat_running:true, access_ready:true, reader_ready:true}, initialization:{state:'idle', action:'none', description:'合成连接已就绪'}, scope_generation:generation, scope_id:'synthetic_scope', scope_switching:false, scope_limits:{total:50}, active_scope:{label:'合成关注范围', count:2, private_count:1, group_count:1}, profiles:[], catalog:{...catalog, description:'明确合成目录状态'}, workspace_path:'/', connection:{state:'connected', reason:'windows_account_mapped_database_matched', description:'明确合成连接，未读取微信。', can_manage:true, can_modify_scope:true, can_read_saved:true, can_start:false, can_recheck:true, actions:['recheck'], checks:{process_running:true, account_verified:true, reader_ready:true, scope_selected:true, collecting:true}, collector_running:true, collector_stage:'collecting'}});
const diagnostics = () => ({schema_version:1, kind:'chatlens_windows_connection_diagnostics', platform:'windows', connection_state:'connected', account_reason:'windows_account_mapped_database_matched', checks:setup().connection.checks, account_diagnostics:{schema_version:1, query_completed:true, mapped_file_query_used:true, mapped_verification_passes:2}, selected_conversation_count:2, collector_running:true, collector_stage:'collecting', catalog:{...catalog, ...(unsafeDiagnostics ? {private_path:'SYNTHETIC_FORBIDDEN_EXTRA_FIELD'} : {})}});
const send = (response, status, value) => {response.writeHead(status, {'Content-Type':'application/json; charset=utf-8', 'Cache-Control':'no-store'}); response.end(JSON.stringify(value));};
const staticFiles = {'/connect.html':['connect.html','text/html'], '/connect.js':['connect.js','text/javascript'], '/connect.css':['connect.css','text/css'], '/styles.css':['styles.css','text/css'], '/assets/chatlens-mark.svg':['assets/chatlens-mark.svg','image/svg+xml']};
catalog = makeCatalog('idle');
const server = http.createServer((request, response) => {
  const url = new URL(request.url, 'http://127.0.0.1');
  if (staticFiles[url.pathname]) {const [name, type] = staticFiles[url.pathname]; response.writeHead(200, {'Content-Type':type, 'Cache-Control':'no-store'}); response.end(fs.readFileSync(path.join(root, 'apps/web', name))); return;}
  if (url.pathname === '/api/session') return send(response, 200, {platform:'windows', scope_generation:generation});
  if (url.pathname === '/api/setup') {counts.setup++; return send(response, 200, setup());}
  if (url.pathname === '/api/setup/catalog' && request.method === 'POST') {
    counts.catalog++;
    if (postMode === 'reject') return send(response, 409, {error:{code:'windows_scope_catalog_not_ready'}});
    catalog = makeCatalog(postMode);
    return send(response, 200, setup());
  }
  if (url.pathname === '/api/setup/conversations') {
    counts.conversations++;
    const requestGeneration = generation, requestRows = rows();
    if (listMode === 'held') {held = () => send(response, 200, {scope_generation:requestGeneration, conversations:requestRows, has_more:false, catalog:makeCatalog('ready')}); heldReady?.(); return;}
    if (listMode === 'error') return send(response, 503, {error:{code:'synthetic_list_unavailable'}});
    if (listMode === 'loading') {catalog = makeCatalog('loading'); return send(response, 200, {conversations:[], has_more:false, catalog});}
    const q = url.searchParams.get('q') || '';
    return send(response, 200, {scope_generation:generation, conversations:listMode === 'empty' ? [] : requestRows.filter(row => row.name.includes(q)), has_more:false, catalog:makeCatalog('ready')});
  }
  if (url.pathname === '/api/setup/diagnostics') return send(response, 200, diagnostics());
  return send(response, 404, {error:{code:'synthetic_route_unavailable'}});
});

async function main() {
  let browser, page;
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = 'http://127.0.0.1:' + server.address().port;
  const check = async (name, action) => {await action(); checks.push(name);};
  try {
    browser = await chromium.launch({headless:true, ...(process.env.CHATLENS_BROWSER_EXECUTABLE ? {executablePath:process.env.CHATLENS_BROWSER_EXECUTABLE} : {})});
    const context = await browser.newContext({viewport:{width:1440, height:1000}, reducedMotion:'reduce'});
    await context.route('**/*', route => {const url = new URL(route.request().url()); if (url.origin !== origin && url.protocol !== 'data:') {issues.external++; return route.abort();} return route.continue();});
    page = await context.newPage(); page.on('pageerror', () => issues.runtime++);
    const id = name => page.locator('#' + name), shot = name => page.screenshot({path:path.join(output, name + '.png'), fullPage:true});
    const beginSearch=async()=>{await id('manage-conversations').click();await id('setup-conversation-search').fill('\u5408\u6210');await id('search-conversations').click();};
    const phase = value => page.waitForFunction(value => document.querySelector('#selection-card').dataset.catalogState === value, value);
    const beginLoadingCatalog = async () => {await Promise.all([page.waitForResponse(response => new URL(response.url()).pathname === '/api/setup/catalog' && response.request().method() === 'POST'), beginSearch()]); await phase('loading');};
    const noOverflow = async () => assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
    const open = async ({state = 'idle', post = 'loading', list = 'ready'} = {}) => {
      if (held) {held(); held = null;}
      generation = generationA; catalog = makeCatalog(state); postMode = post; listMode = list; unsafeDiagnostics = false;
      await page.goto(origin + '/connect.html?manage=1&synthetic_case=' + (++scenario) + '#token=explicit_synthetic_token');
      await id('manage-conversations').waitFor();
      await page.waitForFunction(() => !document.querySelector('#manage-conversations').disabled);
    };
    await check('delayed_catalog_readiness_loads_once_without_second_click', async () => {
      await open(); const lists = counts.conversations, posts = counts.catalog;
      await beginLoadingCatalog();
      assert(await id('setup-conversation-search').isEnabled()); assert.equal(counts.conversations, lists);
      assert((await id('conversation-feedback-text').innerText()).includes('正在读取联系人名称'));
      assert((await id('conversation-feedback-text').innerText()).includes('28 条联系人'));
      assert(!(await id('private-list').innerText()).includes('没有匹配'));
      await noOverflow(); await shot('synthetic-desktop-catalog-loading');
      catalog = makeCatalog('ready'); await phase('ready');
      assert.equal(await page.locator('.picker-checkbox').count(), 2); assert.equal(await page.locator('.selected-chip').count(), 2);
      await page.waitForResponse(response => new URL(response.url()).pathname === '/api/setup');
      assert.equal(counts.catalog, posts + 1); assert.equal(counts.conversations, lists + 1); assert(await id('setup-conversation-search').isEnabled());
    });
    await check('catalog_failure_is_visible_and_retry_rebuilds_without_disconnect', async () => {
      await open(); await beginLoadingCatalog();
      catalog = makeCatalog('error'); await phase('error');
      assert(await id('connection-notice').isHidden()); assert((await id('conversation-feedback-text').innerText()).includes('超时'));
      assert.equal(await id('retry-conversations').innerText(), '重新读取会话目录'); assert(await id('retry-conversations').isEnabled());
      await shot('synthetic-desktop-catalog-error');
      const posts = counts.catalog; postMode = 'ready'; await id('retry-conversations').click(); await phase('ready');
      assert.equal(counts.catalog, posts + 1); assert.equal(await page.locator('.selected-chip').count(), 2);
    });
    await check('failed_worker_cleanup_blocks_retry_until_actual_completion', async () => {
      await open(); await beginLoadingCatalog(); catalog = makeCatalog('error', {worker_active:true}); await phase('error');
      assert(await id('retry-conversations').isDisabled()); assert(await id('manage-conversations').isDisabled());
      assert((await id('conversation-feedback-text').innerText()).includes('正在结束本次读取'));
      catalog = makeCatalog('error'); await page.waitForFunction(() => !document.querySelector('#retry-conversations').disabled);
    });
    await check('post_catalog_error_stays_local_and_is_retryable', async () => {
      await open({post:'reject'}); await beginSearch(); await phase('error');
      assert(await id('connection-notice').isHidden()); assert(await id('retry-conversations').isEnabled());
      postMode = 'ready'; await id('retry-conversations').click(); await phase('ready');
    });
    await check('not_ready_empty_response_does_not_seed_or_claim_no_matches', async () => {
      await open({state:'ready', list:'loading'}); await beginSearch(); await phase('loading');
      assert(await id('setup-conversation-search').isEnabled()); assert(await id('start-selected').isDisabled());
      assert(!(await id('private-list').innerText()).includes('没有匹配'));
      listMode = 'ready'; catalog = makeCatalog('ready'); await phase('ready');
      assert.equal(await page.locator('.selected-chip').count(), 2);
    });
    await check('empty_query_stays_search_first_and_filtered_empty_has_clear_feedback', async () => {
      await open({state:'ready', list:'empty'}); await beginSearch(); await phase('ready');
      assert((await id('private-list').innerText()).includes('没有匹配的私聊')); assert(await id('setup-conversation-search').isEnabled());
      await id('setup-conversation-search').fill(''); await phase('search');
      assert((await id('private-list').innerText()).includes('输入名称开始查找'));
      listMode = 'ready'; await id('setup-conversation-search').fill('不存在的合成名称');
      await page.waitForFunction(() => document.querySelector('#private-list').textContent.includes('没有匹配的私聊'));
      assert((await id('private-list').innerText()).includes('清空搜索'));
    });
    await check('list_failure_waits_for_explicit_retry_and_keeps_selection', async () => {
      await open({state:'ready'}); await beginSearch(); await phase('ready');
      listMode = 'error'; await id('setup-conversation-search').fill('客户'); await phase('error');
      const requests = counts.conversations, setups = counts.setup;
      await page.waitForResponse(response => new URL(response.url()).pathname === '/api/setup');
      assert(counts.setup > setups); assert.equal(counts.conversations, requests);
      assert.equal(await id('retry-conversations').innerText(), '重新加载列表'); assert.equal(await page.locator('.selected-chip').count(), 2);
      listMode = 'ready'; await id('retry-conversations').click(); await phase('ready'); assert.equal(await page.locator('.selected-chip').count(), 2);
    });
    await check('late_old_generation_response_cannot_seed_new_scope', async () => {
      await open({state:'ready', list:'held'});
      let heldTimer;
      const requestHeld = new Promise((resolve, reject) => {heldReady = resolve; heldTimer = setTimeout(() => reject(new Error('Synthetic held response was not requested.')), 10000);});
      await beginSearch();
      await page.waitForFunction(() => document.querySelector('#selection-card').dataset.catalogState === 'loading');
      await requestHeld; clearTimeout(heldTimer); heldReady = null;
      generation = generationB; catalog = makeCatalog('idle');
      await page.waitForFunction(() => document.querySelector('#selection-card').hidden && document.querySelectorAll('.selected-chip').length === 0);
      held(); held = null; listMode = 'ready'; postMode = 'ready';
      await beginSearch(); await phase('ready');
      assert.equal(await page.locator('.picker-checkbox').count(), 1); assert.equal(await page.locator('.selected-chip').count(), 0);
      assert((await id('private-list').innerText()).includes('新范围合成客户')); assert(!(await id('group-list').innerText()).includes('合成测试群'));
    });
    await check('390px_loading_error_retry_and_keyboard_controls_fit', async () => {
      await page.setViewportSize({width:390, height:844}); await open(); await beginLoadingCatalog(); await noOverflow(); await shot('synthetic-mobile-catalog-loading');
      catalog = makeCatalog('error'); await phase('error'); await noOverflow(); await shot('synthetic-mobile-catalog-error');
      postMode = 'ready'; await id('retry-conversations').focus(); await page.keyboard.press('Enter'); await phase('ready');
      await id('setup-conversation-search').focus(); await page.keyboard.type('客户'); await page.waitForFunction(() => document.querySelectorAll('.picker-checkbox').length === 1);
      assert.equal(await page.locator('.selected-chip').count(), 2); await noOverflow(); await shot('synthetic-mobile-catalog-ready');
    });
    await check('safe_catalog_diagnostics_download_and_unknown_fields_rejected', async () => {
      const [download] = await Promise.all([page.waitForEvent('download'), id('download-connection-diagnostics').click()]);
      const target = path.join(output, 'synthetic-connection-diagnostics.json'); await download.saveAs(target);
      const downloaded = JSON.parse(fs.readFileSync(target)); assert.equal(downloaded.catalog.state, 'ready'); assert(!Object.hasOwn(downloaded.catalog, 'description'));
      unsafeDiagnostics = true; await page.waitForFunction(() => !document.querySelector('#download-connection-diagnostics').disabled); await id('download-connection-diagnostics').click();
      await page.waitForFunction(() => document.querySelector('#setup-toast').textContent.includes('诊断格式暂不可用'));
    });
    assert.equal(issues.runtime, 0); assert.equal(issues.external, 0); assert.deepEqual(hashes(), before);
    fs.writeFileSync(path.join(output, 'browser-qa.json'), JSON.stringify({status:'passed', scope:'explicit_synthetic_http_picker_contracts_only', passed_check_count:checks.length, checks, viewports:['1440x1000','390x844'], source_sha256:before, browser_runtime_errors:issues.runtime, browser_external_requests:issues.external, real_windows_manager_executed:false, real_windows_api_executed:false, real_wechat_or_cloud_used:false}, null, 2) + '\n');
    process.stdout.write(JSON.stringify({status:'passed', checks:checks.length, output}) + '\n');
  } catch (problem) {if (page) await page.screenshot({path:path.join(output, 'synthetic-failure.png'), fullPage:true}).catch(() => {}); throw problem;}
  finally {if (held) held(); await browser?.close(); await new Promise(resolve => server.close(resolve));}
}
main().catch(problem => {process.stderr.write(String(problem.stack || problem) + '\n'); process.exitCode = 1;});
