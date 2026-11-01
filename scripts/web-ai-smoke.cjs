#!/usr/bin/env node
'use strict';
// Actual AiAssistant + collector-store HTTP + an explicitly synthetic local
// provider. Browser API responses are never mocked; no real cloud is called.
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
const output = path.resolve(option('--output', path.join(root, '.build', 'web-ai-smoke-' + Date.now())));
assert(!fs.existsSync(output), 'Choose a new evidence directory.');
fs.mkdirSync(output, { recursive: true });
const sourceNames = ['apps/web/index.html','apps/web/app.js','apps/web/styles.css','scripts/web-ai-smoke.cjs','tests/web_ai_fixture.py','scripts/ai_service.py','scripts/ai_provider.py','scripts/ai_knowledge.py','scripts/ai_web_server.py','scripts/windows-proof/web_data.py','scripts/windows-proof/web_server.py'];
const hashes = () => Object.fromEntries(sourceNames.map(name => [name, createHash('sha256').update(fs.readFileSync(path.join(root,name))).digest('hex')]));
const before = hashes();
const python = process.env.CHATLENS_PYTHON || path.join(root,'.build/wechat-read-venv/bin/python3.12');
const fixture = spawn(python, [path.join(root,'tests/web_ai_fixture.py')], { cwd: root, stdio: ['pipe','pipe','pipe'] });
fixture.stderr.resume();
const input = readline.createInterface({ input: fixture.stdout }), queue = [], pending = [];
input.on('line', line => { let value; try { value=JSON.parse(line); } catch { value={error:'fixture_invalid_response'}; } const next=pending.shift(); if(next)next(value);else queue.push(value); });
const receive = () => new Promise((resolve,reject) => { if(queue.length){resolve(queue.shift());return;} const reply=value=>{clearTimeout(timer);resolve(value);};const timer=setTimeout(()=>{const i=pending.indexOf(reply);if(i>=0)pending.splice(i,1);reject(new Error('Synthetic fixture timeout.'));},15000);pending.push(reply); });
const command = async value => { fixture.stdin.write(JSON.stringify(value)+'\n'); const result=await receive();assert(!result.error,result.error);return result; };

async function main(){
  let browser,page;
  const checks=[],issues={page:0,external:0};
  let questionRequests=0;
  const check=async(name,fn)=>{await fn();checks.push(name);};
  try{
    const ready=await receive();assert(ready.ready,ready.error||'Fixture failed.');
    const origin=new URL(ready.launch_url).origin;
    browser=await chromium.launch({headless:true,...(process.env.CHATLENS_BROWSER_EXECUTABLE?{executablePath:process.env.CHATLENS_BROWSER_EXECUTABLE}:{})});
    const context=await browser.newContext({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
    await context.route('**/*',route=>{const url=new URL(route.request().url());if(url.origin!==origin&&url.protocol!=='file:'&&url.protocol!=='data:'){issues.external++;return route.abort();}return route.continue();});
    page=await context.newPage();page.on('pageerror',()=>issues.page++);
    page.on('request',request=>{if(new URL(request.url()).pathname==='/api/ai/questions')questionRequests++;});
    const id=name=>page.locator('#'+name);
    const noOverflow=async()=>assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
    const shot=name=>page.screenshot({path:path.join(output,name+'.png'),fullPage:true});
    const navigate=async view=>{if(page.viewportSize().width<=640&&await id('nav-toggle').getAttribute('aria-expanded')!=='true')await id('nav-toggle').click();await id('nav-'+view).click();await id('view-'+view).waitFor();};
    const idle=async()=>{await id('ai-runtime').waitFor({state:'hidden',timeout:20000});await page.waitForFunction(()=>!document.querySelector('#ai-send').disabled,{timeout:15000});};
    const ask=async text=>{await idle();await id('ai-question').fill(text);await id('ai-send').click();};
    const configure=async({base,model,key,remove=false})=>{await id('ai-model-target').click();await id('model-dialog').waitFor();await id('model-preset').selectOption('custom');await id('model-base-url').fill(base);await id('model-name').fill(model);if(remove&&await id('model-delete-key-row').isVisible())await id('model-delete-key').check();if(key)await id('model-api-key').fill(key);await id('model-save').click();await page.waitForFunction(()=>document.querySelector('#model-feedback').textContent.includes('已保存到本机'));};
    await page.goto(ready.launch_url);
    await page.waitForFunction(()=>document.querySelector('#ai-knowledge-label').textContent.includes('126 条'));
    await check('real_http_ai_home_unconfigured_has_no_fabricated_answer_or_cloud_call',async()=>{
      assert.equal(await id('nav-overview').innerText(),'AI 助手');assert.equal(await page.locator('[data-ai-prompt]').count(),4);assert.equal(await page.locator('.ai-turn').count(),0);assert.equal(new URL(page.url()).hash,'');
      assert.equal(await page.evaluate(()=>localStorage.length+sessionStorage.length),0);assert.equal((await command({command:'counts'})).calls,0);await noOverflow();
    });await shot('desktop-unconfigured');
    await check('quick_prompt_only_fills_draft_and_unconfigured_submit_opens_settings',async()=>{
      await page.locator('[data-ai-prompt=followup]').click();assert((await id('ai-question').inputValue()).includes('跟进'));assert.equal(questionRequests,0);await id('ai-send').click();await id('model-dialog').waitFor();assert.equal(await page.locator('.ai-assistant').count(),0);assert.equal(await id('model-base-url').inputValue(),'https://api.minimax.cn/v1');assert.equal((await command({command:'counts'})).calls,0);await id('close-model').click();
    });
    await check('cloud_config_explicit_local_save_key_cleared_no_automatic_test',async()=>{
      await configure({base:'https://api.minimax.cn/v1',model:'MiniMax-M3.1-Flash',key:'SYNTHETIC_KEY_ONLY_NOT_REAL'});
      assert.equal(await id('model-api-key').inputValue(),'');assert.equal((await command({command:'counts'})).calls,0);assert.equal(await page.evaluate(()=>localStorage.length+sessionStorage.length),0);await id('close-model').click();await id('ai-cloud-consent').waitFor();
    });
    await check('visible_cloud_target_consent_required_and_reset_after_scope_or_time_change',async()=>{
      assert((await id('ai-cloud-confirm-label').innerText()).includes('api.minimax.cn'));const beforeRequests=questionRequests;await id('ai-question').fill('合成报价');await id('ai-send').click();assert.equal(questionRequests,beforeRequests);assert((await id('ai-form-notice').innerText()).includes('勾选'));
      await id('ai-cloud-confirm').check();await id('ai-scope').selectOption('private_test');assert.equal(await id('ai-cloud-confirm').isChecked(),false);await id('ai-cloud-confirm').check();await id('ai-time-range').selectOption('all');assert.equal(await id('ai-cloud-confirm').isChecked(),false);assert.equal((await command({command:'counts'})).calls,0);
    });
    await check('local_synthetic_provider_save_and_explicit_test_use_real_http_without_chat',async()=>{
      await configure({base:ready.synthetic_provider_url,model:'synthetic-model',remove:true});await id('model-test').click();await page.waitForFunction(()=>document.querySelector('#model-feedback').textContent.includes('测试通过'),null,{timeout:15000});const count=await command({command:'counts'});assert.equal(count.test_calls,1);assert.equal(count.question_calls,0);await id('close-model').click();assert.equal(await id('ai-cloud-consent').isVisible(),false);await id('ai-scope').selectOption('all');
    });
    await check('actual_retrieval_generation_answer_and_valid_citations',async()=>{
      await ask('合成报价有哪些约定？');await id('ai-runtime').waitFor();await page.waitForFunction(()=>document.querySelector('#ai-stage-text').textContent.includes('生成'),null,{timeout:15000});await idle();assert((await page.locator('.ai-assistant .ai-answer-text').last().innerText()).includes('合成供应商'));assert(await page.locator('.ai-evidence-card').count()>0);assert(await page.locator('.ai-inline-citation').count()>0);assert.equal((await command({command:'counts'})).question_calls,1);
    });await id('ai-chat-scroll').evaluate(node=>{node.scrollTop=0;});await shot('desktop-answer');
    await check('citation_outside_latest100_opens_exact_context_and_literal_source',async()=>{
      await page.locator('.ai-inline-citation').first().click();await id('view-messages').waitFor();await id('citation-context').waitFor();const target=page.locator('.message-row.is-evidence-target');await target.waitFor();assert.equal(await target.getAttribute('data-message-id'),'1');assert((await target.innerText()).includes('<img src=x'));assert.equal(await page.locator('.message-body img,.message-body script').count(),0);
      await command({command:'commit',body:'合成引用期间到达的新文字'});await page.waitForFunction(()=>document.querySelector('#total-messages').textContent==='127 条');assert.equal(await page.locator('.message-row.is-evidence-target').getAttribute('data-message-id'),'1');assert.equal(await page.locator('.message-body').filter({hasText:'合成引用期间到达的新文字'}).count(),0);await id('citation-latest').click();await page.getByText('合成引用期间到达的新文字',{exact:true}).first().waitFor();await navigate('overview');
    });
    await check('multiple_turns_same_thread_history_and_scope_change_new_context',async()=>{
      await ask('这个报价还需要确认什么？');await idle();assert.equal(await page.locator('.ai-user').count(),2);assert.equal(await page.locator('.ai-assistant').count(),2);assert(await page.locator('.ai-history-item').count()>=1);
      await id('ai-scope').selectOption('group_test');assert.equal(await page.locator('.ai-turn').count(),0);await ask('群内交付约定是什么？');await idle();for(const text of await page.locator('.ai-evidence-top strong').allTextContents())assert(text.includes('合成订单测试群'));
    });
    await check('history_reload_restores_original_scope_and_multiple_turns',async()=>{
      await page.locator('.ai-history-item').filter({hasText:'合成报价有哪些约定？'}).click();await page.waitForFunction(()=>document.querySelectorAll('.ai-user').length===2);assert.equal(await id('ai-scope').inputValue(),'all');assert.equal(await id('ai-time-range').inputValue(),'all');assert.equal(await page.locator('.ai-assistant').count(),2);
    });
    await check('no_matching_evidence_is_system_hint_and_does_not_call_provider',async()=>{
      const count=(await command({command:'counts'})).question_calls;await id('ai-new-thread').click();await ask('zzunknownnotfoundword9988');await idle();assert((await page.locator('.ai-system').last().innerText()).includes('证据'));assert.equal((await command({command:'counts'})).question_calls,count);
    });
    await check('cancel_slow_actual_provider_job_has_no_fabricated_completed_answer',async()=>{
      await command({command:'mode',mode:'slow'});await id('ai-new-thread').click();await id('ai-scope').selectOption('all');await ask('合成报价');await page.waitForFunction(()=>document.querySelector('#ai-stage-text').textContent.includes('生成'));await id('ai-cancel').click();await idle();assert((await id('ai-turns').innerText()).includes('已取消'));await command({command:'mode',mode:'ok'});
    });
    await check('actual_provider_auth_error_and_explicit_retry',async()=>{
      await command({command:'mode',mode:'auth'});await id('ai-new-thread').click();await ask('合成报价');await idle();assert((await page.locator('.ai-turn-error').last().innerText()).includes('认证'));await command({command:'mode',mode:'ok'});await page.locator('.ai-turn-error .text-button').last().click();await idle();assert((await page.locator('.ai-assistant .ai-answer-text').last().innerText()).includes('合成供应商'));
    });
    await check('uncited_provider_answer_is_rejected_before_display',async()=>{
      await command({command:'mode',mode:'bad_citation'});await id('ai-new-thread').click();await ask('合成报价');await idle();assert((await page.locator('.ai-turn-error').last().innerText()).includes('引用'));assert.equal(await page.getByText('合成无效引用回答 [E999]',{exact:true}).count(),0);await command({command:'mode',mode:'ok'});
    });
    await check('knowledge_status_reports_actual_collected_range',async()=>{
      await id('ai-knowledge-toggle').click();await id('knowledge-dialog').waitFor();assert.equal(await id('ai-kb-conversations').innerText(),'2 个');assert.equal(await id('ai-kb-messages').innerText(),'127 条');assert.notEqual(await id('ai-kb-baseline').innerText(),'暂无记录');assert((await id('knowledge-dialog').innerText()).includes('不代表完整微信历史'));await id('close-knowledge').click();
    });
    await check('generic_today_shortcut_retrieves_real_collected_fixture_text',async()=>{
      const calls=(await command({command:'counts'})).question_calls;await id('ai-new-thread').click();await page.locator('[data-ai-prompt=today]').click();assert.equal(await id('ai-time-range').inputValue(),'today');assert.equal(await id('ai-question').inputValue(),'今天有哪些重点？');await id('ai-send').click();await idle();assert.equal((await command({command:'counts'})).question_calls,calls+1);assert((await page.locator('.ai-assistant .ai-answer-text').last().innerText()).includes('合成供应商'));
    });
    await check('message_page_switch_filter_search_and_older_pagination',async()=>{
      await navigate('messages');await page.locator('[data-filter=group]').click();assert.equal(await page.locator('.conversation-item').count(),1);await page.locator('.conversation-item').click();assert((await id('thread-title').innerText()).includes('测试群'));await page.locator('[data-filter=all]').click();await page.locator('[data-testid=conversation-item-private_test]').click();await id('load-older').click();await page.waitForFunction(()=>document.querySelectorAll('.message-row').length===126);await id('message-search').fill('合成报价');await page.waitForFunction(()=>document.querySelector('#message-count').textContent==='1 条结果');await id('message-search').fill('');
    });
    await check('new_collector_message_preserves_reading_then_explicit_latest',async()=>{
      await id('message-scroll').evaluate(node=>{node.scrollTop=80;});const top=await id('message-scroll').evaluate(node=>node.scrollTop);await command({command:'commit',body:'合成阅读期间的新消息'});await page.waitForFunction(()=>document.querySelectorAll('.message-body').length===127);assert(Math.abs(await id('message-scroll').evaluate(node=>node.scrollTop)-top)<2);await id('new-messages').waitFor();await id('new-messages').click();await page.waitForFunction(()=>{const n=document.querySelector('#message-scroll');return n.scrollHeight-n.clientHeight-n.scrollTop<5;});
    });
    await check('display_settings_and_actual_collection_stopped_state',async()=>{
      await id('settings-button').click();await id('pref-density').selectOption('compact');await id('save-settings').click();assert(await page.locator('body').evaluate(node=>node.classList.contains('compact')));await command({command:'status',stage:'stopped'});await navigate('collection');await page.waitForFunction(()=>document.querySelector('#collection-stage-title').textContent==='采集已停止');assert.equal(await id('collection-toggle').isVisible(),false);
    });
    await check('390px_ai_drawer_model_dialog_and_message_context_fit',async()=>{
      await page.setViewportSize({width:390,height:844});await navigate('overview');await id('ai-new-thread').click();await noOverflow();await id('toast').waitFor({state:'hidden'});await shot('mobile-ai-empty');await id('ai-model-target').click();await noOverflow();await shot('mobile-model');await id('close-model').click();await id('nav-toggle').click();assert.equal(Math.round((await id('sidebar').boundingBox()).width),245);await page.keyboard.press('Escape');assert.equal(await id('nav-toggle').getAttribute('aria-expanded'),'false');await navigate('messages');await id('mobile-back').click();await page.locator('[data-testid=conversation-item-group_test]').click();await noOverflow();await shot('mobile-messages');
    });
    await check('file_preview_ai_is_explicit_fiction_and_keeps_message_controls',async()=>{
      const preview=await context.newPage();let apiCalls=0;preview.on('request',request=>{if(new URL(request.url()).pathname.startsWith('/api/'))apiCalls++;});preview.on('pageerror',()=>issues.page++);await preview.goto('file://'+path.join(root,'apps/web/index.html'));await preview.locator('[data-ai-prompt=agreement]').click();await preview.locator('#ai-send').click();await preview.getByText('ChatLens · 虚构回答示例',{exact:true}).waitFor();await preview.waitForFunction(()=>document.querySelectorAll('.ai-inline-citation').length===2);assert((await preview.locator('#ai-turns').innerText()).includes('虚构回答示例'));await preview.locator('.ai-inline-citation').first().click();await preview.locator('.message-row.is-evidence-target').waitFor();assert(await preview.locator('#simulate-message').isVisible());assert.equal(apiCalls,0);await preview.close();
    });
    assert.equal(issues.page,0);assert.equal(issues.external,0);assert.deepEqual(hashes(),before);
    const counts=await command({command:'counts'});
    const result={status:'passed',scope:'actual_ai_assistant_collector_http_and_explicit_synthetic_loopback_provider',real_minimax_or_wechat_used:false,checks,passed_check_count:checks.length,source_sha256:before,browser_runtime_errors:issues.page,browser_external_requests:issues.external,synthetic_provider_calls:counts.calls,synthetic_provider_test_calls:counts.test_calls,synthetic_provider_question_calls:counts.question_calls};
    fs.writeFileSync(path.join(output,'browser-qa.json'),JSON.stringify(result,null,2)+'\n');process.stdout.write(JSON.stringify({status:'passed',checks:checks.length,evidence:path.relative(root,output)})+'\n');
  }catch(error){
    if(page)await page.screenshot({path:path.join(output,'failure.png'),fullPage:true}).catch(()=>{});
    fs.writeFileSync(path.join(output,'failure.json'),JSON.stringify({status:'failed',completed:checks,reason:error.message.replace(/http:\/\/127\.0\.0\.1:[0-9]+[^ ]*/g,'[local-url]')},null,2)+'\n');process.stdout.write(JSON.stringify({status:'failed',completed:checks.length,reason:error.message.split('\n')[0],evidence:path.relative(root,output)})+'\n');process.exitCode=1;
  }finally{
    if(browser)await browser.close();
    if(fixture.exitCode===null){try{await command({command:'exit'});}catch{}fixture.stdin.end();const timer=setTimeout(()=>fixture.kill('SIGTERM'),2500);await new Promise(resolve=>fixture.exitCode===null?fixture.once('exit',resolve):resolve());clearTimeout(timer);}
    input.close();
  }
}
main().catch(()=>{process.stdout.write('{"status":"failed","reason":"synthetic_harness_failed"}\n');process.exitCode=1;});
