#!/usr/bin/env node
'use strict';
// Explicit synthetic HTTP contracts for production onboarding and search UI.
// No Windows manager/native API, real account, chat source or cloud is used.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {createHash} = require('node:crypto');
const {chromium} = require(process.env.CHATLENS_PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..'), args = process.argv.slice(2), at = args.indexOf('--output');
const output = path.resolve(at < 0 ? path.join(root, '.build', 'windows-onboarding-' + Date.now()) : args[at + 1]);
assert(!fs.existsSync(output), 'Choose a new evidence directory.'); fs.mkdirSync(output, {recursive:true});
const sources = ['apps/web/connect.js', 'apps/web/connect.html', 'apps/web/connect.css', 'apps/web/styles.css', 'scripts/web-windows-onboarding-smoke.cjs'];
const hashes = () => Object.fromEntries(sources.map(name => [name, createHash('sha256').update(fs.readFileSync(path.join(root, name))).digest('hex')]));
const before = hashes(), checks = [], issues = {runtime:0, external:0};
const generationA = 'synthetic_onboarding_account_A_20261101', generationB = 'synthetic_onboarding_account_B_20261101';
let generation, onboarding, catalog, readyStage, selected, postFailure, moreFailure, holdMore, held, heldReady, caseNumber = 0;
const counts = {initialize:0, cancel:0, import:0, catalog:0, searches:[], empty_queries:0, selections:[]};
const firstRows = [{id:'synthetic_alpha', name:'合成客户甲', type:'private'}, {id:'synthetic_beta', name:'合成客户乙', type:'private'}, {id:'synthetic_group', name:'合成测试群', type:'group'}, {id:'synthetic_gamma', name:'合成客户丙', type:'private'}, {id:'synthetic_old', name:'合成旧关注', type:'private'}];
const rows = () => generation === generationA ? firstRows : [{id:'synthetic_new', name:'合成新账号客户', type:'private'}];
const makeOnboarding = (state = 'needs_initialization', extra = {}) => ({state, mode:'new', can_initialize:['needs_initialization', 'error'].includes(state), can_cancel:state === 'initializing', can_import:false, reason:null, description:'明确合成状态；没有读取真实微信。', progress:{phase:'detecting_account', elapsed_seconds:0}, ...extra});
const makeCatalog = (state = 'ready') => ({schema_version:1, state, phase:state === 'ready' ? 'complete' : state === 'loading' ? 'reading_contacts' : 'idle', reason:null, elapsed_seconds:state === 'loading' ? 2 : 0, worker_active:state === 'loading', databases_checked:3, tables_seen:12001, contacts_seen:24001, conversations_found:5});
function setup() {
  const ready = onboarding.state === 'ready', pending = ['initializing', 'detecting'].includes(onboarding.state), detected = !['waiting_for_wechat', 'ambiguous'].includes(onboarding.state);
  const members = rows().filter(row => selected.includes(row.id));
  return {platform:'windows', stage:ready ? readyStage : 'needs_initialization', account:{detected, display_name:detected ? '当前微信账号' : null}, scope_generation:generation, scope_id:'synthetic_scope', scope_switching:false, scope_limits:{total:50},
    checks:{wechat_running:detected, access_ready:detected, reader_ready:ready}, initialization:{state:pending ? 'working' : 'idle', action:'none', description:'明确合成初始化状态'}, onboarding,
    active_scope:ready && members.length ? {label:'合成原关注范围', count:members.length, private_count:members.filter(row => row.type === 'private').length, group_count:members.filter(row => row.type === 'group').length} : null,
    profiles:[], catalog, workspace_path:'/', connection:{state:ready ? 'connected' : pending ? 'checking_account' : onboarding.state === 'waiting_for_wechat' ? 'waiting_for_wechat' : 'account_unverified', reason:ready ? 'windows_account_mapped_database_matched' : onboarding.reason, description:'明确合成连接状态', can_manage:ready, can_modify_scope:ready, can_read_saved:ready && selected.length > 0, can_start:ready && selected.length > 0, can_recheck:!pending, actions:pending ? [] : ['recheck', ...(ready && selected.length ? ['start'] : [])],
      checks:{process_running:detected, account_verified:detected, reader_ready:ready, scope_selected:ready && selected.length > 0, collecting:false}, collector_running:false, collector_stage:null}};
}
const send = (response, status, value) => {response.writeHead(status, {'Content-Type':'application/json; charset=utf-8', 'Cache-Control':'no-store'}); response.end(JSON.stringify(value));};
const staticFiles = {'/connect.html':['connect.html','text/html'], '/connect.js':['connect.js','text/javascript'], '/connect.css':['connect.css','text/css'], '/styles.css':['styles.css','text/css'], '/assets/chatlens-mark.svg':['assets/chatlens-mark.svg','image/svg+xml']};
const server = http.createServer(async (request, response) => {
  const url = new URL(request.url, 'http://127.0.0.1');
  if (staticFiles[url.pathname]) {const [name, type] = staticFiles[url.pathname]; response.writeHead(200, {'Content-Type':type, 'Cache-Control':'no-store'}); response.end(fs.readFileSync(path.join(root, 'apps/web', name))); return;}
  if (url.pathname === '/api/session') return send(response, 200, {platform:'windows', scope_generation:generation});
  if (url.pathname === '/api/setup') return send(response, 200, setup());
  if (request.method === 'POST') {
    let body = ''; for await (const chunk of request) body += chunk;
    if (request.headers['x-chatlens-scope'] !== generation) return send(response, 409, {error:{code:'scope_changed'}});
    const value = JSON.parse(body || '{}');
    if (url.pathname === '/api/setup/initialize') {
      counts.initialize++; assert.deepEqual(value, {});
      if (postFailure) return send(response, 409, {error:{code:'windows_onboarding_initialization_unavailable'}});
      onboarding = makeOnboarding('initializing', {progress:{phase:'memory_references', elapsed_seconds:7, completed:3, total:8}});
      return send(response, 200, setup());
    }
    if (url.pathname === '/api/setup/cancel') {
      counts.cancel++; assert.deepEqual(value, {}); generation = generationB;
      onboarding = makeOnboarding('initializing', {can_cancel:false, progress:{phase:'cancelling', elapsed_seconds:8}});
      return send(response, 200, setup());
    }
    if (url.pathname === '/api/setup/import') {
      counts.import++; assert.deepEqual(value, {});
      onboarding = makeOnboarding('initializing', {can_cancel:false, progress:{phase:'importing_workspace', elapsed_seconds:0}});
      return send(response, 200, setup());
    }
    if (url.pathname === '/api/setup/catalog') {counts.catalog++; catalog = makeCatalog('loading'); return send(response, 200, setup());}
    if (url.pathname === '/api/setup/select') {counts.selections.push(value.conversation_ids); selected = value.conversation_ids; readyStage = 'ready_to_start'; generation = generationB; return send(response, 200, setup());}
    if (url.pathname === '/api/setup/recheck') return send(response, 200, setup());
  }
  if (url.pathname === '/api/setup/conversations') {
    const query = url.searchParams.get('q') || '', cursor = url.searchParams.get('cursor');
    counts.searches.push({query, cursor:cursor || null}); if (!query) counts.empty_queries++;
    const currentGeneration = generation, matches = rows().filter(row => row.name.includes(query));
    const start = cursor ? Number(cursor.split('_').at(-1)) : 0;
    const payload = {scope_generation:currentGeneration, conversations:matches.slice(start, start + 2), selected_conversations:cursor ? undefined : rows().filter(row => selected.includes(row.id)), has_more:matches.length > start + 2, next_cursor:matches.length > start + 2 ? 'synthetic_cursor_' + (start + 2) : null, catalog};
    if (cursor && moreFailure) return send(response, 409, {error:{code:'windows_scope_cursor_invalid'}});
    if (cursor && holdMore) {held = () => send(response, 200, payload); heldReady?.(); return;}
    return send(response, 200, payload);
  }
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
    const waitState = value => page.waitForFunction(value => document.querySelector('#windows-onboarding-card').dataset.onboardingState === value, value);
    const waitPicker = value => page.waitForFunction(value => document.querySelector('#selection-card').dataset.catalogState === value, value);
    const noOverflow = async () => assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
    const open = async (state = 'ready', mode = 'new', catalogState = 'ready') => {
      held?.(); held = heldReady = null; generation = generationA; onboarding = makeOnboarding(state, {mode}); catalog = makeCatalog(catalogState); readyStage = mode === 'reused' ? 'running' : 'ready_to_select'; selected = mode === 'reused' ? ['synthetic_group', 'synthetic_old'] : []; postFailure = moreFailure = holdMore = false;
      await page.goto(origin + '/connect.html?manage=1&synthetic_case=' + (++caseNumber) + '#token=explicit_synthetic_token');
      await waitState(state);
    };
    const search = async query => {if (await id('selection-card').isHidden()) await id('manage-conversations').click(); await id('setup-conversation-search').fill(query); await id('search-conversations').click(); await waitPicker(query ? 'ready' : 'search');};

    await check('new_computer_waits_for_login_without_directory_picker_or_automatic_initialization', async () => {
      const count = counts.initialize; await open('waiting_for_wechat');
      assert(await id('windows-onboarding-card').isVisible()); assert(await id('windows-initialize').isHidden()); assert(await id('scope-manager').isHidden()); assert.equal(counts.initialize, count); assert((await id('windows-onboarding-storage').innerText()).includes('自动管理')); await noOverflow(); await shot('synthetic-desktop-first-login');
    });
    await check('current_account_requires_explicit_local_initialization_action', async () => {
      onboarding = makeOnboarding('needs_initialization'); await waitState('needs_initialization');
      assert((await id('windows-onboarding-help').innerText()).includes('本人点击初始化后')); assert((await id('windows-onboarding-help').innerText()).includes('进程内存'));
      assert(await id('windows-initialize').isEnabled()); assert(await id('selection-card').isHidden()); const started = counts.initialize;
      await id('windows-initialize').click(); await waitState('initializing'); assert.equal(counts.initialize, started + 1);
      assert((await id('windows-onboarding-count').innerText()).includes('3 / 8')); assert.equal(await id('windows-onboarding-elapsed').innerText(), '00:07'); assert(await id('windows-cancel-initialization').isEnabled()); await shot('synthetic-desktop-initializing');
    });
    await check('cancel_rotates_domain_and_blocks_duplicate_initialization_while_unwinding', async () => {
      const cancelled = counts.cancel; await id('windows-cancel-initialization').click(); await page.waitForFunction(() => document.querySelector('#windows-onboarding-help').textContent.includes('不会重复开始'));
      assert.equal(counts.cancel, cancelled + 1); assert(await id('windows-initialize').isHidden()); assert((await id('windows-onboarding-help').innerText()).includes('不会重复开始'));
      onboarding = makeOnboarding('error', {reason:'windows_onboarding_cancelled'}); await waitState('error'); assert(await id('windows-initialize').isEnabled()); assert(await id('connection-notice').isHidden());
    });
    await check('initialization_request_failure_is_local_retryable_and_does_not_fake_disconnect', async () => {
      await open('needs_initialization'); postFailure = true; await id('windows-initialize').click(); await waitState('error');
      assert(await id('connection-notice').isHidden()); await page.locator('#windows-onboarding-details summary').click(); assert((await id('windows-onboarding-reason').innerText()).includes('windows_onboarding_initialization_unavailable'));
      postFailure = false; await id('windows-initialize').click(); await waitState('initializing');
    });
    await check('successful_first_initialization_opens_search_without_fetching_all_conversations', async () => {
      const queries = counts.searches.length; generation = generationB; onboarding = makeOnboarding('ready'); selected = []; readyStage = 'ready_to_select';
      await waitState('ready'); await id('selection-card').waitFor(); await waitPicker('search');
      assert(await id('setup-conversation-search').isEnabled()); assert.equal(await page.locator('.picker-checkbox').count(), 0); assert.equal(counts.searches.length, queries); assert(await id('start-selected').isDisabled()); await shot('synthetic-desktop-search-first');
    });
    await check('reused_workspace_preserves_legacy_focus_and_skips_initialization', async () => {
      const initialized = counts.initialize; await open('ready', 'reused');
      assert((await id('windows-onboarding-storage').innerText()).includes('复用')); assert(await id('windows-initialize').isHidden()); assert((await id('active-scope-count').innerText()).includes('2 个会话')); assert.equal(counts.initialize, initialized);
    });
    await check('legacy_import_uses_local_action_without_browser_path_or_config_input', async () => {
      await open('error'); onboarding = makeOnboarding('error', {can_import:true, reason:'workspace_legacy_ambiguous'});
      await page.waitForFunction(() => !document.querySelector('#windows-import-data').hidden);
      await id('windows-import-data').click(); await waitState('initializing'); assert(counts.import > 0); assert.equal(await page.locator('input[type=file]').count(), 0); assert((await id('windows-onboarding-phase').innerText()).includes('本机选择'));
    });
    await check('query_during_catalog_build_stays_editable_and_only_latest_name_is_requested', async () => {
      await open('ready', 'new', 'idle'); const first = counts.searches.length;
      await id('setup-conversation-search').fill('旧名称'); await id('search-conversations').click(); await waitPicker('loading'); assert(await id('setup-conversation-search').isEnabled());
      await id('setup-conversation-search').fill('客户'); await page.waitForFunction(() => document.querySelector('#setup-conversation-search').value === '客户');
      catalog = makeCatalog('ready'); await waitPicker('ready');
      assert(counts.searches.length > first); assert(counts.searches.slice(first).every(row => row.query === '客户')); assert.equal(await page.locator('.selected-chip').count(), 0);
    });
    await check('first_query_seeds_all_existing_selection_even_if_outside_loaded_page', async () => {
      await open('ready', 'reused'); await search('合成');
      assert.equal(await page.locator('.picker-checkbox').count(), 2); assert.equal(await page.locator('.selected-chip').count(), 2); assert((await id('selected-conversations').innerText()).includes('合成旧关注')); assert(await id('load-more-conversations').isEnabled());
      await page.locator('.picker-checkbox[data-picker-id=synthetic_alpha]').check(); assert.equal(await page.locator('.selected-chip').count(), 3);
    });
    await check('pagination_search_and_clear_keep_manual_selection_until_explicit_confirmation', async () => {
      const confirmed = counts.selections.length; await id('load-more-conversations').click(); await page.waitForFunction(() => document.querySelectorAll('.picker-checkbox').length === 4);
      await page.locator('.picker-checkbox[data-picker-id=synthetic_gamma]').check(); assert.equal(await page.locator('.selected-chip').count(), 4);
      await search('测试群'); assert.equal(await page.locator('.picker-checkbox').count(), 1); assert.equal(await page.locator('.selected-chip').count(), 4);
      await page.locator('.picker-checkbox[data-picker-id=synthetic_group]').uncheck(); assert.equal(await page.locator('.selected-chip').count(), 3);
      await id('setup-conversation-search').fill(''); await waitPicker('search'); assert.equal(await page.locator('.picker-checkbox').count(), 0); assert.equal(await page.locator('.selected-chip').count(), 3); assert(await id('start-selected').isEnabled()); assert.equal(counts.selections.length, confirmed);
      await id('start-selected').click(); await page.waitForFunction(() => document.querySelector('#selection-card').hidden); assert.equal(counts.selections.length, confirmed + 1); assert.deepEqual(new Set(counts.selections.at(-1)), new Set(['synthetic_old', 'synthetic_alpha', 'synthetic_gamma']));
    });
    await check('cursor_failure_keeps_current_choices_and_has_explicit_retry', async () => {
      await open('ready', 'reused'); await search('合成'); moreFailure = true; await id('load-more-conversations').click(); await page.waitForFunction(() => document.querySelector('#load-more-conversations').textContent.includes('重试'));
      assert.equal(await page.locator('.picker-checkbox').count(), 2); assert.equal(await page.locator('.selected-chip').count(), 2); assert(await id('connection-notice').isHidden());
      moreFailure = false; await id('load-more-conversations').click(); await page.waitForFunction(() => document.querySelectorAll('.picker-checkbox').length === 4);
    });
    await check('account_change_drops_old_choices_and_late_cursor_response', async () => {
      await open('ready', 'reused'); await search('合成'); holdMore = true;
      let timer; const pending = new Promise((resolve, reject) => {heldReady = resolve; timer = setTimeout(() => reject(new Error('Synthetic cursor was not requested.')), 10000);});
      await id('load-more-conversations').click(); await pending; clearTimeout(timer); heldReady = null;
      generation = generationB; onboarding = makeOnboarding('needs_initialization'); selected = [];
      await waitState('needs_initialization'); assert(await id('selection-card').isHidden()); assert.equal(await page.locator('.selected-chip').count(), 0);
      held(); held = null; holdMore = false; onboarding = makeOnboarding('ready'); readyStage = 'ready_to_select'; await waitState('ready'); await search('合成');
      assert.equal(await page.locator('.picker-checkbox').count(), 1); assert((await id('private-list').innerText()).includes('新账号')); assert(!(await id('private-list').innerText()).includes('合成客户甲')); assert.equal(await page.locator('.selected-chip').count(), 0);
    });
    await check('390px_first_run_progress_error_search_and_pagination_are_keyboard_usable', async () => {
      await page.setViewportSize({width:390, height:844}); await open('needs_initialization'); await noOverflow(); await shot('synthetic-mobile-first-initialization');
      await id('windows-initialize').focus(); await page.keyboard.press('Enter'); await waitState('initializing'); await noOverflow(); await shot('synthetic-mobile-initializing');
      onboarding = makeOnboarding('error', {reason:'windows_onboarding_initialization_failed'}); await waitState('error'); await noOverflow(); await shot('synthetic-mobile-initialization-error');
      await open('ready', 'reused'); await id('manage-conversations').click(); await id('setup-conversation-search').focus(); await page.keyboard.type('合成'); await page.keyboard.press('Enter'); await waitPicker('ready');
      await id('load-more-conversations').focus(); await page.keyboard.press('Enter'); await page.waitForFunction(() => document.querySelectorAll('.picker-checkbox').length === 4); await noOverflow(); await shot('synthetic-mobile-search-pagination');
    });
    await check('unsupported_database_layout_explains_compatibility_without_login_or_retry_loop', async () => {
      await open('error'); onboarding = makeOnboarding('error', {reason:'windows_onboarding_database_layout_unsupported', can_initialize:false});
      await page.waitForFunction(() => document.querySelector('#windows-onboarding-description').textContent.includes('数据格式暂未适配'));
      assert(await id('windows-initialize').isHidden()); assert((await id('windows-onboarding-help').innerText()).includes('无需重复初始化')); assert(!(await id('windows-onboarding-description').innerText()).includes('未登录')); await noOverflow(); await shot('synthetic-mobile-layout-unsupported');
    });
    await check('search_first_never_issues_blank_query_and_contract_sources_remain_frozen', async () => {
      assert.equal(counts.empty_queries, 0); assert.equal(issues.runtime, 0); assert.equal(issues.external, 0); assert.deepEqual(hashes(), before);
    });
    fs.writeFileSync(path.join(output, 'browser-qa.json'), JSON.stringify({status:'passed', scope:'explicit_synthetic_http_windows_onboarding_and_search_pagination_contracts_only', passed_check_count:checks.length, checks, viewports:['1440x1000','390x844'], source_sha256:before, browser_runtime_errors:issues.runtime, browser_external_requests:issues.external, empty_query_requests:counts.empty_queries, explicit_initialization_requests:counts.initialize, real_windows_manager_executed:false, real_windows_api_executed:false, real_wechat_or_cloud_used:false}, null, 2) + '\n');
    process.stdout.write(JSON.stringify({status:'passed', checks:checks.length, output}) + '\n');
  } catch (problem) {if (page) await page.screenshot({path:path.join(output, 'synthetic-failure.png'), fullPage:true}).catch(() => {}); throw problem;}
  finally {held?.(); await browser?.close(); await new Promise(resolve => server.close(resolve));}
}
main().catch(problem => {process.stderr.write(String(problem.stack || problem) + '\n'); process.exitCode = 1;});
