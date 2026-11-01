#!/usr/bin/env node
'use strict';
// Production Windows manager/server/store with synthetic process/account/source
// adapters. Browser responses are actual HTTP; no response mocks or real chat.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {spawn}=require('node:child_process'),readline=require('node:readline');
const {createHash}=require('node:crypto');
const {chromium}=require(process.env.CHATLENS_PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..'),args=process.argv.slice(2),at=args.indexOf('--output');
const output=path.resolve(at<0?path.join(root,'.build','windows-connection-'+Date.now()):args[at+1]);
assert(!fs.existsSync(output),'Choose a new evidence directory.');fs.mkdirSync(output,{recursive:true});
const names=['apps/web/index.html','apps/web/app.js','apps/web/styles.css','apps/web/connect.html','apps/web/connect.js','apps/web/connect.css',
 'scripts/web-windows-connection-smoke.cjs','tests/web_windows_multi_fixture.py','tests/web_ai_fixture.py',
 'scripts/windows-proof/windows_web_manager.py','scripts/windows-proof/windows_web_server.py','scripts/windows-proof/web_runtime.py',
 'scripts/windows-proof/windows_account_probe.py','scripts/windows-proof/windows_process_detection.py','scripts/windows-proof/windows_mapped_files.py'];
const hashes=()=>Object.fromEntries(names.map(name=>[name,createHash('sha256').update(fs.readFileSync(path.join(root,name))).digest('hex')]));
const before=hashes(),checks=[],issues={page:0,external:0};
const check=async(name,fn)=>{await fn();checks.push(name);};

async function fixture(mode){
 const child=spawn(process.env.CHATLENS_PYTHON||path.join(root,'.build/wechat-read-venv/bin/python'),[path.join(root,'tests/web_windows_multi_fixture.py'),mode],{cwd:root,stdio:['pipe','pipe','pipe']});
 child.stderr.resume();const lines=readline.createInterface({input:child.stdout}),pending=[],queue=[];
 lines.on('line',line=>{let value;try{value=JSON.parse(line);}catch{value={error:'synthetic_windows_connection_protocol_error'};}const next=pending.shift();if(next)next(value);else queue.push(value);});
 const receive=()=>new Promise((resolve,reject)=>{if(queue.length)return resolve(queue.shift());const done=value=>{clearTimeout(timer);resolve(value);};const timer=setTimeout(()=>reject(new Error('Synthetic Windows connection fixture timed out.')),15000);pending.push(done);});
 const command=async value=>{child.stdin.write(JSON.stringify(value)+'\n');const result=await receive();assert(!result.error,result.error);return result;};
 const ready=await receive();assert(ready.ready,ready.error);assert(ready.network_guard.self_test_passed);
 return{ready,command,async close(){await command({command:'exit'});child.stdin.end();}};
}

async function main(){
 let browser,page,current;
 try{
  browser=await chromium.launch({headless:true,...(process.env.CHATLENS_BROWSER_EXECUTABLE?{executablePath:process.env.CHATLENS_BROWSER_EXECUTABLE}:{})});
  async function open(mode){
   current=await fixture(mode);const origin=new URL(current.ready.launch_url).origin;
   const context=await browser.newContext({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
   await context.route('**/*',route=>{const url=new URL(route.request().url());if(url.origin!==origin&&url.protocol!=='data:'){issues.external++;return route.abort();}return route.continue();});
   context.on('page',p=>p.on('pageerror',()=>issues.page++));page=await context.newPage();await page.goto(current.ready.launch_url);
   await page.locator('#windows-connection-card').waitFor();return context;
  }
  const id=name=>page.locator('#'+name),command=value=>current.command(value);
  const shot=name=>page.screenshot({path:path.join(output,name+'.png'),fullPage:true});
  const noOverflow=async()=>assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
  const api=async(route,value,generation)=>page.evaluate(async({route,value,generation})=>{if(generation===undefined)generation=(await(await fetch('/api/session',{credentials:'same-origin'})).json()).scope_generation;const response=await fetch(route,{credentials:'same-origin',headers:{'X-ChatLens-Scope':generation,...(value===undefined?{}:{'Content-Type':'application/json'})},...(value===undefined?{}:{method:'POST',body:JSON.stringify(value)})});return{status:response.status,body:await response.json()};},{route,value,generation});
  const setup=async()=>(await api('/api/setup')).body;
  const guide=async()=>{if(new URL(page.url()).pathname!=='/connect.html')await id('workspace-connection-guide').click();await id('windows-connection-card').waitFor();};
  const workspace=async()=>{await page.waitForURL(url=>url.pathname==='/');await page.waitForFunction(()=>document.querySelector('#ai-knowledge-label').textContent.includes('6 条'));};
  const searchAll=async(p=page)=>{await p.locator('#setup-conversation-search').fill('\u5408\u6210');await p.locator('#search-conversations').click();await p.locator('input.picker-checkbox').first().waitFor();};
  const waitTitle=async text=>page.waitForFunction(text=>document.querySelector('#windows-connection-title')?.textContent.includes(text),text,{timeout:15000});
  const readyState=async()=>{await waitTitle('核验');await page.waitForFunction(()=>document.querySelector('#windows-start').hidden===false&&!document.querySelector('#windows-start').disabled);};
  const start=async()=>{await id('windows-start').click();await workspace();};
  let context=await open('--wait-for-wechat'),oldNonce;
  await check('normal_start_without_wechat_opens_five_step_guide_and_keeps_saved_data',async()=>{
   await waitTitle('等待本机微信');assert.equal(new URL(page.url()).pathname,'/connect.html');assert.equal(new URL(page.url()).hash,'');assert.equal(await page.locator('[data-windows-check]').count(),5);assert(await id('initialization-card').isHidden());assert(await id('scope-manager').isVisible());assert(await id('manage-conversations').isDisabled());assert(await id('scope-manage-help').isVisible());assert((await id('scope-manage-help').innerText()).length>10);assert(await id('windows-recheck').isEnabled());assert(await id('windows-offline').isVisible());assert.equal((await command({command:'state'})).children_started,0);assert.equal((await command({command:'state'})).legacy_unchanged,true);await noOverflow();await shot('desktop-waiting-for-wechat');
  });
  await check('offline_workspace_explicitly_explains_saved_messages_and_has_connection_entry',async()=>{
   await id('windows-offline').click();await workspace();await id('windows-workspace-connection').waitFor();assert((await id('workspace-connection-title').innerText()).includes('微信'));assert(await id('scope-manage-sidebar').isVisible());assert.equal((await api('/api/status')).body.total_messages,6);assert.equal((await api('/api/status')).body.state,'stopped');await shot('desktop-offline-workspace');await guide();
  });
  await check('recheck_without_wechat_is_retryable_and_does_not_start_or_reset',async()=>{
   await id('windows-recheck').click();await waitTitle('等待本机微信');await page.waitForFunction(()=>!document.querySelector('#windows-recheck').disabled);assert.equal((await command({command:'state'})).children_started,0);assert.equal((await command({command:'state'})).legacy_unchanged,true);assert.equal((await setup()).connection.can_modify_scope,false);
  });
  await check('missing_account_reading_evidence_is_neutral_and_keeps_verified_process_fact',async()=>{
   await command({command:'account_mode',state:'unknown'});await command({command:'process',state:'ready'});await waitTitle('账号读取核验未完成');assert((await page.locator('[data-windows-check="process_running"] .windows-check-state').innerText()).includes('已找到'));assert((await page.locator('[data-windows-check="account_verified"] .windows-check-state').innerText()).includes('尚未'));assert((await id('windows-connection-description').innerText()).includes('读取定位'));assert((await id('windows-next-step').innerText()).includes('保持当前状态'));assert(!/进入微信|扫码|完成登录后|放开.*权限/.test(await id('windows-next-step').innerText()));assert(await id('manage-conversations').isDisabled());assert(await id('windows-recheck').isEnabled());assert.equal((await command({command:'state'})).children_started,0);await shot('desktop-account-reading-unverified');await page.setViewportSize({width:390,height:844});await noOverflow();assert.equal(await id('windows-connection-title').innerText(),'账号读取核验未完成');await shot('mobile-account-reading-unverified');await page.setViewportSize({width:1440,height:1000});
  });
  await check('unverified_account_can_download_only_safe_cached_diagnostic_counts',async()=>{
   const button=id('download-connection-diagnostics');await button.waitFor();assert(await button.isEnabled());
   const [download]=await Promise.all([page.waitForEvent('download'),button.click()]);
   assert.equal(download.suggestedFilename(),'ChatLens-Windows-Connection-Diagnostics.json');
   const target=path.join(output,download.suggestedFilename());await download.saveAs(target);
   const raw=fs.readFileSync(target,'utf8'),value=JSON.parse(raw);
   assert.deepEqual(Object.keys(value).sort(),['schema_version','kind','platform','connection_state','account_reason','checks','account_diagnostics','selected_conversation_count','collector_running','collector_stage','catalog'].sort());
   assert.equal(value.kind,'chatlens_windows_connection_diagnostics');assert.equal(value.schema_version,1);
   assert.equal(value.connection_state,'account_unverified');assert.equal(value.account_reason,'windows_account_mapped_database_unavailable');
   assert.equal(value.account_diagnostics.handles_seen,0);assert.equal(value.account_diagnostics.file_type_handles,0);assert.equal(value.account_diagnostics.query_completed,true);
   assert.equal(value.account_diagnostics.mapped_file_query_used,true);assert.equal(value.account_diagnostics.mapped_verification_passes,2);assert.equal(value.account_diagnostics.mapped_regions_queried,160);assert.equal(value.account_diagnostics.mapped_data_regions_seen,12);assert.equal(value.account_diagnostics.mapped_name_query_failures,0);
   assert.equal(value.account_diagnostics.family_process_count,5);assert.equal(value.account_diagnostics.family_processes_completed,5);assert.equal(value.account_diagnostics.main_database_names_seen,0);assert.equal(value.account_diagnostics.helper_database_names_seen,0);
   assert.deepEqual(Object.keys(value.catalog).sort(),['schema_version','state','phase','reason','elapsed_seconds','worker_active','databases_checked','tables_seen','contacts_seen','conversations_found'].sort());
   assert.equal(value.catalog.schema_version,1);assert.equal(value.catalog.state,'idle');assert.equal(value.catalog.phase,'idle');assert.equal(value.catalog.reason,null);assert.equal(value.catalog.worker_active,false);
   for(const key of ['elapsed_seconds','databases_checked','tables_seen','contacts_seen','conversations_found'])assert.equal(value.catalog[key],0);
   assert(value.checks.process_running);assert(!value.checks.account_verified);assert.equal(value.selected_conversation_count,2);
   assert(!/synthetic-peer|synthetic-group|scope_generation|creation_time|\\\\|\/Users\/|wxid_|enc_key|api_key|"pid"|"path"/.test(raw));
   assert.equal((await command({command:'state'})).children_started,0);assert.equal((await command({command:'state'})).legacy_unchanged,true);
  });
  await check('login_in_same_service_enables_scope_management_without_automatic_collection',async()=>{
   await command({command:'account_mode',state:'matched'});await readyState();assert(await id('manage-conversations').isEnabled());assert.equal((await command({command:'state'})).children_started,0);assert.equal((await setup()).connection.can_start,true);await id('manage-conversations').click();await searchAll();assert.equal(await page.locator('input.picker-checkbox').count(),60);assert.equal(await page.locator('input.picker-checkbox:checked').count(),2);assert.equal((await command({command:'state'})).children_started,0);await shot('desktop-ready-and-can-manage');
  });
  await check('explicit_start_restores_original_scope_without_changing_baseline_or_messages',async()=>{
   await start();assert.equal((await command({command:'state'})).children_started,1);assert.equal((await command({command:'state'})).legacy_unchanged,true);assert.equal((await api('/api/status')).body.state,'collecting');oldNonce=(await api('/api/session')).body.scope_generation;await noOverflow();await shot('desktop-collecting-workspace');
  });
  await check('starting_owned_heartbeat_does_not_claim_ready_or_allow_duplicate_start',async()=>{
   await command({command:'collector_phase',stage:'starting'});await guide();await waitTitle('准备');const value=await setup();assert.equal(value.connection.collector_running,true);assert.equal(value.connection.collector_stage,'starting');assert.equal(value.connection.checks.collecting,false);assert.equal(value.connection.can_start,false);await noOverflow();await shot('desktop-owned-starting');
  });
  await check('paused_owned_heartbeat_is_distinct_from_stopped_and_stays_actionable',async()=>{
   await command({command:'collector_phase',stage:'paused'});await waitTitle('暂停');const value=await setup();assert.equal(value.connection.collector_stage,'paused');assert.equal(value.connection.collector_running,true);assert.equal(value.connection.can_start,false);assert.equal(value.connection.checks.collecting,false);await shot('desktop-owned-paused');await command({command:'collector_phase',stage:'collecting'});await page.waitForFunction(()=>!document.querySelector('#windows-open-workspace').hidden);await id('windows-open-workspace').click();await workspace();
  });
  await check('stopped_collector_has_actionable_banner_and_can_restart_in_same_web_service',async()=>{
   await command({command:'end_child'});await page.waitForFunction(()=>document.querySelector('#workspace-connection-title')?.textContent.includes('停止'));await guide();await page.waitForFunction(()=>!document.querySelector('#windows-start').disabled);await start();assert.equal((await command({command:'state'})).children_started,2);assert.equal((await command({command:'state'})).legacy_unchanged,true);
  });
  await check('wechat_exit_clears_old_page_nonce_and_returns_to_waiting_with_offline_browse',async()=>{
   oldNonce=(await api('/api/session')).body.scope_generation;await command({command:'process',state:'not_running'});await page.waitForURL(url=>url.pathname==='/connect.html');await waitTitle('等待本机微信');assert.equal((await api('/api/messages',undefined,oldNonce)).status,409);assert.equal((await setup()).connection.can_read_saved,true);assert.equal((await setup()).connection.checks.collecting,false);assert.equal((await command({command:'state'})).legacy_unchanged,true);
  });
  await check('another_account_is_distinct_from_not_logged_in_and_never_reuses_old_domain',async()=>{
   await command({command:'account_mode',state:'mismatch'});await command({command:'process',state:'ready',pid:777,creation_time:888});await waitTitle('不匹配');const value=await setup();assert.equal(value.connection.state,'wrong_account');assert.deepEqual(value.profiles,[]);assert.equal(value.connection.can_read_saved,false);assert(await id('windows-offline').isHidden());assert(await id('manage-conversations').isDisabled());assert(await id('windows-recheck').isEnabled());await shot('desktop-wrong-account');
  });
  await check('original_account_returns_with_new_pid_and_resumes_authorized_checkpoint',async()=>{
   await command({command:'account_mode',state:'matched'});await page.waitForFunction(()=>!document.querySelector('#windows-open-workspace').hidden);await id('windows-open-workspace').click();await workspace();assert.equal((await command({command:'state'})).children_started,3);assert.equal((await command({command:'state'})).legacy_unchanged,true);assert.equal((await api('/api/status')).body.total_messages,6);assert.notEqual((await api('/api/session')).body.scope_generation,oldNonce);
  });
  await check('390px_waiting_ready_and_scope_steps_have_visible_actions_without_overflow',async()=>{
   await command({command:'process',state:'not_running'});await page.waitForURL(url=>url.pathname==='/connect.html');await waitTitle('等待本机微信');await page.setViewportSize({width:390,height:844});await noOverflow();await shot('mobile-waiting-guide');await command({command:'process',state:'ready',pid:778,creation_time:889});await page.waitForFunction(()=>!document.querySelector('#windows-open-workspace').hidden);await id('manage-conversations').click();await searchAll();await noOverflow();await shot('mobile-connected-scope-picker');assert.equal((await command({command:'state'})).legacy_unchanged,true);
  });
  assert.equal((await command({command:'state'})).provider_calls,0);
  await context.close();await current.close();current=null;
  context=await open('--read-only');
  await check('explicit_read_only_is_named_and_explains_why_scope_change_is_disabled',async()=>{
   await waitTitle('离线只读');assert(await id('windows-recheck').isHidden());assert(await id('windows-start').isHidden());assert(await id('windows-offline').isVisible());assert(await id('scope-manager').isVisible());assert(await id('manage-conversations').isDisabled());assert((await id('scope-manage-help').innerText()).includes('只读'));assert.equal((await command({command:'state'})).children_started,0);await page.setViewportSize({width:390,height:844});await noOverflow();await shot('mobile-explicit-read-only');
  });
  await check('explicit_offline_workspace_preserves_management_link_and_next_step_description',async()=>{
   await id('windows-offline').click();await workspace();assert((await id('workspace-connection-title').innerText()).includes('只读'));await id('nav-toggle').click();assert(await id('scope-manage-sidebar').isVisible());await id('scope-manage-sidebar').click();await waitTitle('离线只读');assert.equal((await command({command:'state'})).legacy_unchanged,true);assert.equal((await command({command:'state'})).provider_calls,0);
  });
  await context.close();await current.close();current=null;
  assert.equal(issues.page,0);assert.equal(issues.external,0);assert.deepEqual(hashes(),before,'Frozen source files changed.');
  const result={status:'passed',scope:'Windows waiting-login-to-collection production manager HTTP UI with explicit synthetic native/source adapters',passed_check_count:checks.length,checks,viewports:['1440x1000','390x844'],browser_runtime_errors:issues.page,browser_external_requests:issues.external,source_sha256:before,real_windows_api_executed:false,real_chat_or_cloud_used:false,permanent_read_only_recovery_verified_synthetically:true};
  fs.writeFileSync(path.join(output,'browser-qa.json'),JSON.stringify(result,null,2)+'\n');process.stdout.write(JSON.stringify({status:'passed',checks:checks.length,output})+'\n');
 }catch(error){if(page)await page.screenshot({path:path.join(output,'synthetic-failure.png'),fullPage:true}).catch(()=>{});throw error;}
 finally{await browser?.close();await current?.close().catch(()=>{});}
}
main().catch(error=>{process.stderr.write(String(error.stack||error)+'\n');process.exitCode=1;});
