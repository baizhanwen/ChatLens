#!/usr/bin/env node
'use strict';
// Production onboarding coordinator, workspace, handlers, collector store and AI
// persistence. Only Windows discovery, initialization and message source are
// explicit synthetic adapters. Browser API bodies are never intercepted.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {spawn} = require('node:child_process');
const readline = require('node:readline');
const {createHash} = require('node:crypto');
const {chromium} = require(process.env.CHATLENS_PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..'), args = process.argv.slice(2), at = args.indexOf('--output');
const output = path.resolve(at < 0 ? path.join(root, '.build', 'windows-first-run-' + Date.now()) : args[at + 1]);
assert(!fs.existsSync(output), 'Choose a new evidence directory.'); fs.mkdirSync(output, {recursive:true});
const sources = ['apps/web/connect.js', 'apps/web/connect.html', 'apps/web/connect.css', 'apps/web/index.html', 'apps/web/app.js', 'apps/web/styles.css', 'scripts/web-windows-first-run-smoke.cjs', 'tests/web_windows_onboarding_fixture.py', 'tests/test_windows_onboarding.py', 'scripts/windows-proof/windows_onboarding.py', 'scripts/windows-proof/windows_workspace.py', 'scripts/windows-proof/windows_web_manager.py', 'scripts/windows-proof/windows_web_server.py', 'scripts/windows-proof/web_runtime.py', 'scripts/windows-proof/continuous_store.py', 'scripts/windows-proof/collector_service.py', 'scripts/windows-proof/web_data.py', 'scripts/ai_service.py', 'scripts/ai_web_server.py'];
sources.push('scripts/windows-proof/windows_onboarding_native.py', 'tests/test_windows_selected_runner.py', 'tests/web_ai_fixture.py', 'scripts/windows-proof/collector_private.py', 'scripts/windows-proof/collector_web_meta.py', 'scripts/windows-proof/conversation_scope.py', 'scripts/windows-proof/windows_selected_engine.py', 'scripts/windows-proof/windows_account_probe.py', 'scripts/windows-proof/windows_process_detection.py', 'scripts/windows-proof/windows_mapped_files.py', 'scripts/windows-proof/windows_keyproof_core.py', 'scripts/windows-proof/windows_keyproof_platform.py', 'scripts/windows-proof/web_server.py', 'scripts/ai_provider.py', 'scripts/ai_statistics.py', 'scripts/ai_query.py', 'scripts/ai_knowledge.py');
const hashes = () => Object.fromEntries(sources.map(name => [name, createHash('sha256').update(fs.readFileSync(path.join(root, name))).digest('hex')]));
const before = hashes(), checks = [], issues = {runtime:0, external:0}, allowedOrigins = new Set(), listQueries = [];
const fixture = spawn(process.env.CHATLENS_PYTHON || path.join(root, '.build/wechat-read-venv/bin/python'), [path.join(root, 'tests/web_windows_onboarding_fixture.py')], {cwd:root, stdio:['pipe', 'pipe', 'pipe']});
fixture.stderr.resume();
const input = readline.createInterface({input:fixture.stdout}), pending = [], queued = [];
input.on('line', line => {let value; try {value = JSON.parse(line);} catch {value = {error:'synthetic_onboarding_protocol_error'};} const next = pending.shift(); if (next) next(value); else queued.push(value);});
const receive = () => new Promise((resolve, reject) => {if (queued.length) return resolve(queued.shift()); const done = value => {clearTimeout(timer); resolve(value);}; const timer = setTimeout(() => {const index = pending.indexOf(done); if (index >= 0) pending.splice(index, 1); reject(new Error('Synthetic onboarding fixture timed out.'));}, 15000); pending.push(done);});
const command = async value => {fixture.stdin.write(JSON.stringify(value) + '\n'); const result = await receive(); assert(!result.error, result.error); return result;};

async function main() {
  let browser, page, origin, originalNonce, firstBaseline, configuredFingerprint, localThread;
  const check = async (name, action) => {await action(); checks.push(name);};
  try {
    const ready = await receive(); assert(ready.ready, ready.error); assert(ready.network_guard.self_test_passed);
    origin = new URL(ready.launch_url).origin; allowedOrigins.add(origin);
    browser = await chromium.launch({headless:true, ...(process.env.CHATLENS_BROWSER_EXECUTABLE ? {executablePath:process.env.CHATLENS_BROWSER_EXECUTABLE} : {})});
    const context = await browser.newContext({viewport:{width:1440, height:1000}, reducedMotion:'reduce'});
    await context.route('**/*', route => {const url = new URL(route.request().url()); if (!allowedOrigins.has(url.origin) && url.protocol !== 'data:') {issues.external++; return route.abort();} return route.continue();});
    page = await context.newPage(); page.on('pageerror', () => issues.runtime++);
    page.on('request', request => {const url = new URL(request.url()); if (url.pathname === '/api/setup/conversations') listQueries.push(url.searchParams.get('q') || '');});
    const id = name => page.locator('#' + name), shot = name => page.screenshot({path:path.join(output, name + '.png'), fullPage:true});
    const noOverflow = async () => assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
    const waitOnboarding = status => page.waitForFunction(status => document.querySelector('#windows-onboarding-card')?.dataset.onboardingState === status, status, {timeout:15000});
    const api = async (route, value, generation) => page.evaluate(async ({route, value, generation}) => {
      if (generation === undefined) generation = (await (await fetch('/api/session', {credentials:'same-origin'})).json()).scope_generation;
      const response = await fetch(route, {credentials:'same-origin', headers:{'X-ChatLens-Scope':generation, ...(value === undefined ? {} : {'Content-Type':'application/json'})}, ...(value === undefined ? {} : {method:'POST', body:JSON.stringify(value)})});
      return {status:response.status, body:await response.json()};
    }, {route, value, generation});
    const navigate = async view => {if (page.viewportSize().width <= 640 && await id('nav-toggle').getAttribute('aria-expanded') !== 'true') await id('nav-toggle').click(); await id(view === 'settings' ? 'settings-button' : 'nav-' + view).click(); await id('view-' + view).waitFor();};
    const waitWorkspace = async () => {await page.waitForURL(url => url.pathname === '/', {timeout:15000}); await id('ai-knowledge-label').waitFor();};
    const startExisting = async () => {await waitOnboarding('ready'); if (new URL(page.url()).pathname === '/connect.html') {await page.waitForFunction(() => !document.querySelector('#windows-start').hidden && !document.querySelector('#windows-start').disabled); await id('windows-start').click();} await waitWorkspace();};
    const search = async query => {if (await id('selection-card').isHidden()) await id('manage-conversations').click(); await id('setup-conversation-search').fill(query); await id('search-conversations').click(); await page.waitForFunction(() => document.querySelector('#selection-card').dataset.catalogState === 'ready');};
    const choose = async name => page.locator('.picker-row').filter({hasText:name}).locator('input.picker-checkbox').check();
    const waitController = async predicate => {const deadline = Date.now() + 10000; while (Date.now() < deadline) {const state = await command({command:'state'}); if (predicate(state)) return state; await new Promise(resolve => setTimeout(resolve, 40));} throw new Error('Synthetic onboarding state did not settle.');};
    await page.goto(ready.launch_url);
    await check('fresh_machine_uses_managed_workspace_and_waits_for_explicit_initialization', async () => {
      await waitOnboarding('needs_initialization'); const state = await command({command:'state'});
      assert.equal(state.workspace_account_count, 0); assert.equal(state.initialization_calls, 0); assert.equal(state.collector_running, false); assert(await id('windows-initialize').isEnabled()); assert(await id('scope-manager').isHidden()); await noOverflow(); await shot('synthetic-actual-desktop-first-use');
      const candidateNonce = state.scope_generation;
      await command({command:'account', account:'two'});
      assert.equal((await api('/api/setup/recheck', {}, candidateNonce)).status, 200);
      const second = await waitController(value => value.onboarding_state === 'needs_initialization' && value.scope_generation !== candidateNonce);
      const rejected = await api('/api/setup/initialize', {}, candidateNonce);
      assert.equal(rejected.status, 409); assert.equal(rejected.body.error.code, 'scope_changed'); assert.equal(second.initialization_calls, 0); assert.equal(second.workspace_account_count, 0);
      await command({command:'account', account:'one'});
      assert.equal((await api('/api/setup/recheck', {}, second.scope_generation)).status, 200);
      const returned = await waitController(value => value.onboarding_state === 'needs_initialization' && value.scope_generation !== second.scope_generation);
      await page.waitForResponse(response => new URL(response.url()).pathname === '/api/setup' && response.status() === 200 && response.request().headers()['x-chatlens-scope'] === returned.scope_generation);
      await waitOnboarding('needs_initialization'); assert.equal(returned.initialization_calls, 0); assert.equal(returned.workspace_account_count, 0);
    });
    await check('explicit_initialization_persists_account_reader_then_shows_search_first', async () => {
      await id('windows-initialize').click(); await waitOnboarding('ready'); const state = await command({command:'state'});
      assert.equal(state.workspace_account_count, 1); assert.equal(state.initialization_calls, 1); assert.equal(state.selected_conversation_count, 0); assert.equal(state.collector_running, false); assert(state.original_reader_files_preserved);
      await id('selection-card').waitFor(); assert.equal(await page.locator('.picker-checkbox').count(), 0); assert.equal(listQueries.length, 0); assert(await id('setup-conversation-search').isEnabled());
    });
    await check('search_matches_are_unselected_until_user_confirms_two_conversations', async () => {
      await search('合成'); assert.equal(await page.locator('.picker-checkbox').count(), 3); assert.equal(await page.locator('.selected-chip').count(), 0);
      await choose('合成客户甲'); await choose('合成测试群'); assert.equal(await page.locator('.selected-chip').count(), 2); assert.equal((await command({command:'state'})).selected_conversation_count, 0); await shot('synthetic-actual-desktop-confirm-focus');
      await id('start-selected').click(); await waitWorkspace(); const state = await command({command:'state'}); assert.equal(state.selected_conversation_count, 2); assert.equal(state.collector_stage, 'collecting'); assert.equal(state.current_message_count, 0); firstBaseline = state.baseline_completed_at; originalNonce = state.scope_generation;
    });
    await check('new_synthetic_message_reaches_production_store_and_web_without_refresh', async () => {
      await command({command:'commit', body:'合成首次接入的新文字'});
      await page.waitForFunction(() => document.querySelector('#ai-knowledge-label').textContent.includes('1 条'));
      const status = await api('/api/status'); assert.equal(status.status, 200); assert.equal(status.body.total_messages, 1); await navigate('messages'); await page.waitForFunction(() => document.querySelector('#view-messages').textContent.includes('合成首次接入的新文字')); await noOverflow(); await shot('synthetic-actual-desktop-new-message');
    });
    await check('local_model_configuration_and_statistics_history_are_saved_without_cloud', async () => {
      await navigate('settings'); await id('model-preset').selectOption('custom'); await id('model-base-url').fill('https://synthetic.invalid/v1'); await id('model-name').fill('explicit-synthetic-first-run-model'); await id('model-api-key').fill('explicit-synthetic-not-real'); await id('model-save').click(); await page.waitForFunction(() => document.querySelector('#model-feedback').textContent.includes('已保存到本机'));
      const configuration = await api('/api/ai/config'); assert.equal(configuration.status, 200); assert.equal(configuration.body.configured, true); configuredFingerprint = configuration.body.target_fingerprint;
      await navigate('overview'); await id('ai-question').fill('今天总共有多少条消息'); await page.waitForFunction(() => !document.querySelector('#ai-send').disabled);
      const posted = page.waitForResponse(response => new URL(response.url()).pathname === '/api/ai/questions' && response.request().method() === 'POST'); await id('ai-send').click(); const result = await (await posted).json(); assert(result.thread_id); localThread = result.thread_id;
      await page.waitForFunction(() => document.querySelector('#ai-runtime').hidden && document.querySelectorAll('.ai-turn').length >= 2, null, {timeout:15000}); const history = await api('/api/ai/threads/' + localThread); assert.equal(history.status, 200); assert.equal(history.body.messages.at(-1).role, 'assistant'); assert.equal(history.body.messages.at(-1).response_kind, 'statistics'); assert.equal(history.body.messages.at(-1).model, null);
    });
    await check('relaunch_reuses_original_data_configuration_history_and_checkpoint', async () => {
      const restarted = await command({command:'relaunch'}); origin = new URL(restarted.launch_url).origin; allowedOrigins.add(origin); await page.goto(restarted.launch_url); await waitOnboarding('ready'); assert((await id('windows-onboarding-storage').innerText()).includes('复用')); assert.equal(restarted.initialization_calls, 1); assert(restarted.original_reader_files_preserved);
      await startExisting(); const state = await command({command:'state'}); assert.equal(state.current_message_count, 1); assert.equal(state.selected_conversation_count, 2); assert.equal(state.baseline_completed_at, firstBaseline); const configuration = await api('/api/ai/config'); assert.equal(configuration.body.target_fingerprint, configuredFingerprint); assert.equal((await api('/api/ai/threads/' + localThread)).status, 200); await shot('synthetic-actual-desktop-reused-workspace');
    });
    await check('different_account_has_new_nonce_and_cannot_read_previous_account_data', async () => {
      const oldNonce = (await command({command:'state'})).scope_generation; await command({command:'account', account:'two'}); await page.waitForURL(url => url.pathname === '/connect.html', {timeout:15000}); await waitOnboarding('needs_initialization'); assert.equal((await api('/api/messages', undefined, oldNonce)).status, 409); assert.equal(await page.locator('.selected-chip').count(), 0); assert(await id('profiles-card').isHidden());
      await id('windows-initialize').click(); await waitOnboarding('ready'); await search('合成客户乙'); await choose('合成客户乙'); await id('start-selected').click(); await waitWorkspace(); const state = await command({command:'state'}); assert.equal(state.workspace_account_count, 2); assert.equal(state.initialization_calls, 2); assert.equal(state.selected_conversation_count, 1); assert.equal(state.current_message_count, 0); assert.notEqual(state.scope_generation, oldNonce); assert.equal((await api('/api/ai/config')).body.configured, false); assert.deepEqual((await api('/api/ai/threads')).body.threads, []);
    });
    await check('returning_account_recovers_its_own_workspace_and_rejects_both_old_nonces', async () => {
      const secondNonce = (await command({command:'state'})).scope_generation; await command({command:'account', account:'one'}); await page.waitForURL(url => url.pathname === '/connect.html', {timeout:15000}); await startExisting(); const state = await command({command:'state'}); assert.equal(state.initialization_calls, 2); assert.equal(state.selected_conversation_count, 2); assert.equal(state.current_message_count, 1); assert.equal(state.baseline_completed_at, firstBaseline); assert(state.original_reader_files_preserved); for (const nonce of [secondNonce, originalNonce]) assert.equal((await api('/api/messages', undefined, nonce)).status, 409); assert.equal((await api('/api/ai/config')).body.target_fingerprint, configuredFingerprint); assert.equal((await api('/api/ai/threads/' + localThread)).status, 200);
    });
    await check('390px_waiting_login_and_reused_search_navigation_fit', async () => {
      await page.setViewportSize({width:390, height:844}); await command({command:'account', account:'waiting'}); await page.waitForURL(url => url.pathname === '/connect.html', {timeout:15000}); await waitOnboarding('waiting_for_wechat'); await noOverflow(); await shot('synthetic-actual-mobile-waiting-login');
      await command({command:'account', account:'one'}); await waitOnboarding('ready'); await search('合成'); assert.equal(await page.locator('.selected-chip').count(), 2); await noOverflow(); await shot('synthetic-actual-mobile-reused-search');
    });
    const state = await command({command:'state'}); assert.equal(state.real_windows_or_cloud_used, false); assert.equal(state.network_guard.blocked_attempts, 1); assert(listQueries.length > 0 && listQueries.every(Boolean)); assert.equal(issues.runtime, 0); assert.equal(issues.external, 0); assert.deepEqual(hashes(), before);
    fs.writeFileSync(path.join(output, 'browser-qa.json'), JSON.stringify({status:'passed', scope:'actual_windows_onboarding_workspace_manager_http_store_ai_persistence_with_explicit_synthetic_windows_and_source_adapters', passed_check_count:checks.length, checks, viewports:['1440x1000','390x844'], source_sha256:before, browser_runtime_errors:issues.runtime, browser_external_requests:issues.external, empty_query_requests:0, original_reader_files_preserved:state.original_reader_files_preserved, original_checkpoint_preserved:true, account_workspace_count:state.workspace_account_count, explicit_initialization_calls:state.initialization_calls, network_guard:state.network_guard, api_responses_intercepted:false, real_windows_api_executed:false, real_wechat_or_cloud_used:false}, null, 2) + '\n');
    process.stdout.write(JSON.stringify({status:'passed', checks:checks.length, output}) + '\n');
  } catch (problem) {if (page) await page.screenshot({path:path.join(output, 'synthetic-failure.png'), fullPage:true}).catch(() => {}); throw problem;}
  finally {await browser?.close(); if (fixture.exitCode === null) {try {await command({command:'exit'});} catch {} fixture.stdin.end(); const timer = setTimeout(() => fixture.kill('SIGTERM'), 3000); await new Promise(resolve => fixture.exitCode === null ? fixture.once('exit', resolve) : resolve()); clearTimeout(timer);} input.close();}
}
main().catch(problem => {process.stderr.write(String(problem.stack || problem) + '\n'); process.exitCode = 1;});
