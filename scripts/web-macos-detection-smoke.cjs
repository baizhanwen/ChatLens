#!/usr/bin/env node
'use strict';
// Explicit synthetic HTTP state contracts; no native probe, WeChat, private
// workspace, account, message, credential or model is used by this test.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {createHash} = require('node:crypto');
const {chromium} = require(process.env.CHATLENS_PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..'), args = process.argv.slice(2), at = args.indexOf('--output');
const output = path.resolve(at < 0 ? path.join(root, '.build', 'macos-detection-' + Date.now()) : args[at + 1]);
assert(!fs.existsSync(output), 'Choose a new evidence directory.');
fs.mkdirSync(output, {recursive:true});
const sources = ['apps/web/connect.js', 'apps/web/connect.html', 'apps/web/connect.css', 'apps/web/styles.css', 'scripts/web-macos-detection-smoke.cjs'];
const hashes = () => Object.fromEntries(sources.map(name => [name, createHash('sha256').update(fs.readFileSync(path.join(root, name))).digest('hex')]));
const before = hashes(), checks = [], issues = {runtime:0, external:0}, counts = {setup:0, mutations:0};
const generation = 'synthetic_mac_detection_generation_20261102';
const scenarios = {
  stopped: {running:false, reason:'macos_active_process_not_running', description:'未找到本机微信程序，请本人打开微信。', title:'未检测到本机微信程序', current:'process'},
  denied: {running:true, reason:'macos_active_path_unavailable', description:'暂时无法访问微信资料目录，请检查当前运行应用的完全磁盘访问权限。', title:'微信已运行，当前账号尚未核验', current:'account'},
  unverified: {running:true, reason:'macos_active_account_not_identified', description:'已找到微信程序，但尚未取得当前账号的活动读取依据。请保持微信登录后重新检查。', title:'微信已运行，当前账号尚未核验', current:'account'},
  ambiguous: {running:true, reason:'macos_active_process_ambiguous', description:'发现多个微信程序，暂不能唯一核验当前账号。', title:'微信已运行，当前账号尚未核验', current:'account'},
  unknown: {running:null, reason:'macos_active_process_probe_failed', description:'本机微信状态检测暂时不可用，请重新检查状态。', title:'暂时无法确认本机微信状态', current:'process'},
  version: {running:true, reason:'macos_active_client_version_changed', description:'当前微信版本与读取配置不一致，暂停并保留原资料。', title:'微信已运行，当前账号尚未核验', current:'account'},
  rebind: {running:true, reason:'macos_active_process_rebind_unavailable', description:'新的微信进程身份尚未核验，暂停并保留原资料。', title:'微信已运行，当前账号尚未核验', current:'account'},
  initialize: {running:true, detected:true, reason:null, description:'已识别当前登录账号，首次使用需要本人确认初始化。', title:'当前登录微信已识别', current:'initialize'},
  initerror: {running:true, detected:true, stage:'error', reason:'macos_reader_initialization_failed', description:'本机初始化未完成，请查看实际原因。', title:'当前登录微信已识别', current:'initialize'},
  select: {running:true, detected:true, reader:true, stage:'ready_to_select', reason:null, description:'本机读取能力已准备好，请选择关注会话。', title:'当前登录微信已识别', current:'select'},
  starting: {running:true, detected:true, reader:true, stage:'starting', selected:2, reason:null, description:'正在准备所选范围，等待微信保持后台。', title:'当前登录微信已识别', current:'collect'},
  running: {running:true, detected:true, reader:true, stage:'running', selected:2, reason:null, description:'采集器已启动，消息以实际保存结果为准。', title:'当前登录微信已识别', current:'collect'}
};
let scenario = scenarios.stopped, setupVersion = 0;
const setup = () => ({platform:'macos', stage:scenario.stage || 'needs_initialization', reason:scenario.reason, account:{detected:scenario.detected === true, display_name:null}, checks:{wechat_running:scenario.running, access_ready:scenario.detected === true ? true : scenario.running === false ? null : false, reader_ready:scenario.reader === true}, initialization:{state:'idle', action:'none', description:scenario.description}, scope_generation:generation, scope_id:'synthetic_scope', scope_switching:false, scope_limits:{total:50}, active_scope:scenario.selected ? {label:'明确合成关注范围', count:scenario.selected, private_count:1, group_count:1} : null, profiles:[], catalog:{state:scenario.reader ? 'ready' : 'idle'}, workspace_path:'/'});
const send = (response, status, value) => {response.writeHead(status, {'Content-Type':'application/json; charset=utf-8', 'Cache-Control':'no-store'}); response.end(JSON.stringify(value));};
const staticFiles = {'/connect.html':['connect.html','text/html'], '/connect.js':['connect.js','text/javascript'], '/connect.css':['connect.css','text/css'], '/styles.css':['styles.css','text/css'], '/assets/chatlens-mark.svg':['assets/chatlens-mark.svg','image/svg+xml']};
const server = http.createServer((request, response) => {
  const url = new URL(request.url, 'http://127.0.0.1');
  if (request.method !== 'GET') {counts.mutations++; return send(response, 409, {error:{code:'synthetic_mutation_forbidden'}});}
  if (staticFiles[url.pathname]) {const [name, type] = staticFiles[url.pathname]; response.writeHead(200, {'Content-Type':type, 'Cache-Control':'no-store'}); response.end(fs.readFileSync(path.join(root, 'apps/web', name))); return;}
  if (url.pathname === '/api/session') return send(response, 200, {platform:'macos', scope_generation:generation});
  if (url.pathname === '/api/setup') {counts.setup++; return send(response, 200, setup());}
  if (url.pathname === '/api/setup/conversations') return send(response, 200, {scope_generation:generation, conversations:[{id:'synthetic_old_choice', name:'明确合成旧会话', type:'private'}], has_more:false, catalog:{state:'ready'}});
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
    const id = name => page.locator('#' + name);
    const shot = name => page.screenshot({path:path.join(output, name + '.png'), fullPage:true});
    const noOverflow = async () => assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
    const show = async (name) => {
      scenario = scenarios[name]; setupVersion++;
      await page.goto(origin + '/connect.html?manage=1&synthetic_case=' + setupVersion + '#token=explicit_synthetic_token');
      await page.waitForFunction(title => document.querySelector('#account-title').textContent === title, scenario.title);
      await page.waitForFunction(step => document.querySelector('#setup-steps [aria-current="step"]')?.dataset.step === step, scenario.current);
    };
    const assertFlow = async () => {
      assert.equal(await page.locator('#setup-steps li').count(), 5);
      assert.equal(await page.locator('#mac-connection-checks li').count(), 5);
      assert(await id('windows-connection-card').isHidden()); assert(await id('windows-sidebar-steps').isHidden());
      assert(await id('check-status').isVisible());
      assert.equal(await id('account-description').innerText(), scenario.description);
      assert.equal(await page.locator('#mac-connection-checks [aria-current="step"]').getAttribute('data-mac-check'), scenario.current);
      if (!scenario.detected) {assert(await id('initialize-button').isDisabled()); assert(!(await id('account-title').innerText()).includes('等待本机微信登录'));}
      if (!scenario.reader) assert(await id('scope-manager').isHidden());
      await noOverflow();
    };
    for (const viewport of [{width:1440,height:1000}, {width:390,height:844}]) {
      await page.setViewportSize(viewport);
      for (const name of Object.keys(scenarios)) {
        await check(viewport.width + 'px_' + name + '_actual_diagnostics_and_five_step_progress', async () => {
          await show(name); await assertFlow();
          if (scenario.reason) {await id('mac-account-details').locator('summary').click(); assert((await id('mac-account-reason').innerText()).includes(scenario.reason));}
          if (['denied', 'select', 'running'].includes(name)) await shot('synthetic-' + viewport.width + '-' + name);
        });
      }
      await check(viewport.width + 'px_manual_recheck_recovers_to_new_account_without_mutation', async () => {
        await show('denied'); const requests = counts.setup;
        scenario = scenarios.initialize;
        await id('check-status').click();
        await page.waitForFunction(() => document.querySelector('#account-title').textContent === '当前登录微信已识别' && !document.querySelector('#check-status').disabled);
        assert(counts.setup > requests); assert(await id('initialize-button').isEnabled());
        assert(await id('mac-account-details').isHidden()); assert.equal(counts.mutations, 0);
        assert.equal(await page.locator('#setup-steps [aria-current="step"]').getAttribute('data-step'), 'initialize'); await noOverflow();
      });
      await check(viewport.width + 'px_automatic_poll_recovers_without_click_or_mutation', async () => {
        await show('unknown'); scenario = scenarios.initialize;
        await page.waitForFunction(() => document.querySelector('#account-title').textContent === '当前登录微信已识别');
        assert(await id('initialize-button').isEnabled()); assert.equal(counts.mutations, 0); await noOverflow();
      });
      await check(viewport.width + 'px_same_generation_detection_failure_clears_old_picker_and_detached_checkbox', async () => {
        await show('select');
        await id('setup-conversation-search').fill('合成');
        const checkbox = page.locator('input.picker-checkbox'); await checkbox.waitFor(); await checkbox.check();
        assert.equal(await page.locator('.selected-chip').count(), 1);
        const detached = await checkbox.elementHandle();
        scenario = scenarios.denied; await id('check-status').click();
        await page.waitForFunction(() => document.querySelector('#selection-card').hidden && document.querySelectorAll('.selected-chip').length === 0);
        assert(!(await page.locator('body').innerText()).includes('明确合成旧会话'));
        scenario = scenarios.select; await id('check-status').click();
        await page.waitForFunction(() => !document.querySelector('#selection-card').hidden && !document.querySelector('#check-status').disabled);
        await detached.evaluate(node => {node.checked = true; node.dispatchEvent(new Event('change', {bubbles:true}));});
        assert.equal(await page.locator('.selected-chip').count(), 0);
        assert.equal(counts.mutations, 0); await noOverflow();
      });
      await check(viewport.width + 'px_unrecognized_or_unsafe_reason_is_not_displayed', async () => {
        await show('unknown');
        for (const reason of ['macos_unknown_synthetic_private_extra', '/synthetic/forbidden-path', '<img src=x onerror=alert(1)>']) {
          scenario = {...scenarios.unknown, reason}; await id('check-status').click();
          await page.waitForFunction(() => !document.querySelector('#check-status').disabled);
          assert(await id('mac-account-details').isHidden()); assert.equal(await id('mac-account-reason').innerText(), '');
        }
        assert.equal(await page.locator('#mac-account-details img').count(), 0); await noOverflow();
      });
    }
    assert.equal(counts.mutations, 0); assert.equal(issues.runtime, 0); assert.equal(issues.external, 0); assert.deepEqual(hashes(), before);
    fs.writeFileSync(path.join(output, 'browser-qa.json'), JSON.stringify({status:'passed', scope:'explicit_synthetic_http_macos_detection_and_five_step_ui_only', passed_check_count:checks.length, checks, viewports:['1440x1000','390x844'], source_sha256:before, browser_runtime_errors:issues.runtime, browser_external_requests:issues.external, mutation_requests:counts.mutations, real_macos_manager_executed:false, real_native_probe_executed:false, real_wechat_or_cloud_used:false}, null, 2) + '\n');
    process.stdout.write(JSON.stringify({status:'passed', checks:checks.length, output}) + '\n');
  } catch (problem) {if (page) await page.screenshot({path:path.join(output, 'synthetic-failure.png'), fullPage:true}).catch(() => {}); throw problem;}
  finally {await browser?.close(); await new Promise(resolve => server.close(resolve));}
}
main().catch(problem => {process.stderr.write(String(problem.stack || problem) + '\n'); process.exitCode = 1;});
