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
const sourceNames = ['apps/web/index.html','apps/web/app.js','apps/web/styles.css','scripts/web-ai-smoke.cjs','tests/web_ai_fixture.py','scripts/ai_service.py','scripts/ai_provider.py','scripts/ai_knowledge.py','scripts/ai_query.py','scripts/ai_statistics.py','scripts/ai_web_server.py','scripts/windows-proof/web_data.py','scripts/windows-proof/wechat_identity.py','scripts/windows-proof/web_server.py'];
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
  let questionRequests=0,planRequests=0;
  const check=async(name,fn)=>{await fn();checks.push(name);};
  try{
    const ready=await receive();assert(ready.ready,ready.error||'Fixture failed.');assert.equal(ready.network_guard?.loopback_only,true);assert.equal(ready.network_guard?.self_test_passed,true);
    const origin=new URL(ready.launch_url).origin;
    browser=await chromium.launch({headless:true,...(process.env.CHATLENS_BROWSER_EXECUTABLE?{executablePath:process.env.CHATLENS_BROWSER_EXECUTABLE}:{})});
    const context=await browser.newContext({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
    await context.route('**/*',route=>{const url=new URL(route.request().url());if(url.origin!==origin&&url.protocol!=='file:'&&url.protocol!=='data:'){issues.external++;return route.abort();}return route.continue();});
    page=await context.newPage();page.on('pageerror',()=>issues.page++);
    page.on('request',request=>{const route=new URL(request.url()).pathname;if(route==='/api/ai/questions')questionRequests++;if(route==='/api/ai/plan')planRequests++;});
    const id=name=>page.locator('#'+name);
    const noOverflow=async()=>assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
    const welcomeFits=async()=>{const bounds=await id('ai-chat-scroll').boundingBox();for(const item of await page.locator('#ai-welcome .ai-prompt,#ai-welcome-boundary').all()){if(!await item.isVisible())continue;const box=await item.boundingBox();assert(box.y>=bounds.y-1&&box.y+box.height<=bounds.y+bounds.height+1,'Cloud confirmation must not clip the empty-state prompts or helper line at '+page.viewportSize().width+'px.');}};
    const shot=name=>page.screenshot({path:path.join(output,name+'.png'),fullPage:true});
    const navigate=async view=>{if(page.viewportSize().width<=640&&await id('nav-toggle').getAttribute('aria-expanded')!=='true')await id('nav-toggle').click();await id(view==='settings'?'settings-button':'nav-'+view).click();await id('view-'+view).waitFor();};
    const idle=async()=>{await id('ai-runtime').waitFor({state:'hidden',timeout:20000});await page.waitForFunction(()=>!document.querySelector('#ai-send').disabled,{timeout:15000});};
    const ask=async text=>{await idle();await id('ai-question').fill(text);await id('ai-send').click();};
    const draft=async(text,route)=>{await id('ai-question').fill(text);await page.waitForFunction(label=>document.querySelector('#ai-plan-preview').hidden===false&&document.querySelector('#ai-plan-route').textContent===label,({local_stats:'本机统计',analysis:'模型分析',mixed:'统计与分析',clarify:'需要确认'})[route],{timeout:10000});};
    const api=async(route,value)=>page.evaluate(async({route,value})=>{const response=await fetch(route,{credentials:'same-origin',...(value===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(value)})});return{status:response.status,body:await response.json()};},{route,value});
    const savedAnswer=async title=>{const listed=await api('/api/ai/threads');assert.equal(listed.status,200);const thread=listed.body.threads.find(row=>row.title===title);assert(thread,'The completed synthetic thread must be saved.');const detail=await api('/api/ai/threads/'+thread.id);assert.equal(detail.status,200);return detail.body.messages.at(-1);};
    const configure=async({base,model,key,remove=false})=>{await navigate('settings');await id('model-form').waitFor();await id('model-preset').selectOption('custom');await id('model-base-url').fill(base);await id('model-name').fill(model);if(remove&&await id('model-delete-key-row').isVisible())await id('model-delete-key').check();if(key)await id('model-api-key').fill(key);await id('model-save').click();await page.waitForFunction(()=>document.querySelector('#model-feedback').textContent.includes('已保存到本机'));assert(await id('view-settings').isVisible());};
    await page.goto(ready.launch_url);
    await page.waitForFunction(()=>document.querySelector('#ai-knowledge-label').textContent.includes('126 条'));
    await check('real_http_ai_home_unconfigured_has_no_fabricated_answer_or_cloud_call',async()=>{
      assert.equal(await id('nav-overview').innerText(),'AI 助手');assert.equal(await page.locator('[data-ai-prompt]').count(),4);assert.equal(await page.locator('.ai-turn').count(),0);assert.equal(new URL(page.url()).hash,'');
      assert.equal(await page.evaluate(()=>localStorage.length+sessionStorage.length),0);assert.equal((await command({command:'counts'})).calls,0);await noOverflow();
      assert.equal(await id('ai-model-target').evaluate(node=>node.tagName),'SPAN');assert.notEqual(await id('ai-model-target').getAttribute('role'),'button');assert.equal(await id('ai-model-target').getAttribute('tabindex'),null);assert.equal(await page.locator('#model-dialog,#settings-dialog').count(),0);
    });await shot('desktop-unconfigured');
    await check('quick_prompt_only_fills_draft_and_unconfigured_submit_opens_settings',async()=>{
      await page.locator('[data-ai-prompt=followup]').click();const draft=await id('ai-question').inputValue();assert(draft.includes('跟进'));assert.equal(questionRequests,0);await id('ai-send').click();await id('view-settings').waitFor();assert.equal(await page.locator('.ai-assistant').count(),0);assert.equal(await id('model-base-url').inputValue(),'https://api.minimax.cn/v1');assert.equal((await command({command:'counts'})).calls,0);await navigate('overview');assert.equal(await id('ai-question').inputValue(),draft);
    });
    await check('local_plan_preview_resolves_question_without_model_request_or_history_write',async()=>{
      const beforeThreads=(await api('/api/ai/threads')).body.threads;const submitted=questionRequests;await id('ai-time-range').selectOption('7d');await draft('今天都谁给我发信息了','local_stats');assert((await id('ai-plan-description').innerText()).includes('今天'));assert.equal(await id('ai-cloud-consent').isVisible(),false);assert.equal(questionRequests,submitted);assert.deepEqual((await api('/api/ai/threads')).body.threads,beforeThreads);assert.equal((await command({command:'counts'})).calls,0);
    });
    await check('verified_identity_projects_own_incoming_and_unknown_without_raw_ids',async()=>{
      const result=await api('/api/messages?conversation=private_test&after=122');assert.equal(result.status,200);const byMessage=new Map(result.body.messages.map(row=>[row.id,row]));assert.equal(byMessage.get(123).direction,'incoming');assert.equal(byMessage.get(123).own,false);assert.equal(byMessage.get(124).direction,'unknown');assert.equal(byMessage.get(124).own,false);assert.equal(byMessage.get(125).direction,'outgoing');assert.equal(byMessage.get(125).own,true);assert.equal(byMessage.get(125).sender,'我');assert(!JSON.stringify(result.body).includes('synthetic-verified-self'));assert(!JSON.stringify(result.body).includes('private_peer_id'));await navigate('messages');await page.locator('[data-testid=conversation-item-private_test]').click();for(const [messageId,direction,label] of [[123,'incoming','收到'],[124,'unknown','收发方向未确认'],[125,'outgoing','本人']]){const row=page.locator('.message-row[data-message-id="'+messageId+'"]');await row.waitFor();assert.equal(await row.getAttribute('data-direction'),direction);assert.equal(await row.locator('.message-direction-label').innerText(),label);assert.equal(await row.evaluate(node=>node.classList.contains('own')),direction==='outgoing');}await shot('desktop-message-directions');await navigate('overview');assert.equal((await command({command:'counts'})).calls,0);
    });
    await check('unconfigured_sender_message_and_ranking_questions_query_all_actual_rows',async()=>{
      for(const [question,metric] of [['今天都谁给我发信息了','senders'],['今天共有多少条消息','messages'],['今天谁最活跃','ranking']]){await id('ai-new-thread').click();await ask(question);await idle();assert(await id('view-overview').isVisible());assert((await page.locator('.ai-assistant .ai-turn-header').last().innerText()).includes('本机统计'));const answer=await savedAnswer(question);assert.equal(answer.response_kind,'statistics');assert.equal(answer.plan.metric,metric);assert.equal(answer.statistics.message_count,metric==='senders'?124:126);assert.equal(answer.statistics.sender_count,metric==='senders'?1:2);assert.equal(answer.statistics.conversation_count,2);assert.equal(answer.model,null);assert.equal(answer.statistics.identity_verified,true);assert.equal(await id('ai-cloud-consent').isVisible(),false);if(metric==='senders'){assert.equal(answer.statistics.direction_applied,'incoming');assert.equal(answer.statistics.unclassified_direction_messages,1);assert(!answer.content.includes('无法排除本人'));}}assert.equal((await command({command:'counts'})).calls,0);await shot('desktop-local-statistics');
    });
    await check('verified_own_count_works_without_model_and_excludes_unknown_sender',async()=>{
      await id('ai-new-thread').click();await ask('今天我发了多少条消息');await idle();const answer=await savedAnswer('今天我发了多少条消息');assert.equal(answer.plan.route,'local_stats');assert.equal(answer.statistics.direction_applied,'outgoing');assert.equal(answer.statistics.identity_verified,true);assert.equal(answer.statistics.message_count,1);assert.equal(answer.statistics.sender_count,1);assert.equal(answer.statistics.conversation_count,1);assert.equal(answer.model,null);assert.equal((await command({command:'counts'})).calls,0);
    });
    await check('local_direction_breakdown_separates_received_sent_and_unknown_counts',async()=>{
      await id('ai-new-thread').click();await ask('今天收到和发出分别有多少条消息');await idle();const answer=await savedAnswer('今天收到和发出分别有多少条消息');assert.equal(answer.plan.route,'local_stats');assert.equal(answer.statistics.group_by,'direction');assert.equal(answer.statistics.message_count,126);assert.equal(answer.statistics.incoming_count,124);assert.equal(answer.statistics.outgoing_count,1);assert.equal(answer.statistics.unknown_direction_count,1);assert.deepEqual(Object.fromEntries(answer.statistics.rows.map(row=>[row.direction,row.message_count])),{incoming:124,outgoing:1,unknown:1});assert.equal((await command({command:'counts'})).calls,0);await shot('desktop-direction-statistics');
    });
    await check('natural_today_and_yesterday_followup_show_actual_dates_in_local_history',async()=>{
      await id('ai-new-thread').click();await ask('今天有几条消息');await idle();await ask('那昨天呢');await idle();const answer=await savedAnswer('今天有几条消息');assert.equal(answer.plan.metric,'messages');assert.equal(answer.plan.time_label,'昨天');assert.equal(answer.statistics.message_count,0);const localYesterday=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(Date.now()-86400000));assert((await page.locator('.ai-turn-plan').last().innerText()).includes('昨天'));assert(answer.plan.summary.includes(localYesterday));await id('ai-new-thread').click();await page.locator('.ai-history-item').filter({hasText:'今天有几条消息'}).click();await page.waitForFunction(()=>document.querySelectorAll('.ai-user').length===2);assert((await page.locator('.ai-assistant .ai-turn-header').last().innerText()).includes('本机统计'));assert((await page.locator('.ai-turn-plan').last().innerText()).includes(localYesterday));assert.equal((await command({command:'counts'})).calls,0);
    });
    await check('outside_ui_time_clarifies_without_config_or_cloud_call',async()=>{
      await id('ai-new-thread').click();await id('ai-time-range').selectOption('today');await draft('上个月一共有多少条消息','clarify');assert(await id('ai-plan-clarification').isVisible());await id('ai-send').click();await idle();const answer=await savedAnswer('上个月一共有多少条消息');assert.equal(answer.role,'system');assert.equal(answer.plan.route,'clarify');assert.equal(answer.model??null,null);assert(await id('view-overview').isVisible());assert(await page.locator('.ai-system').count()>0);assert.equal((await command({command:'counts'})).calls,0);
    });
    await check('local_statistics_selected_scope_and_evidence_stay_in_original_conversation',async()=>{
      await id('ai-new-thread').click();await id('ai-scope').selectOption('private_test');await id('ai-time-range').selectOption('7d');await ask('今天共有多少条信息');await idle();const answer=await savedAnswer('今天共有多少条信息');assert.equal(answer.statistics.message_count,125);assert.equal(answer.statistics.conversation_count,1);assert(answer.evidence.every(row=>row.conversation_id==='private_test'));await page.locator('.ai-inline-citation').first().click();await id('citation-context').waitFor();assert((await id('thread-title').innerText()).includes('知秋'));assert.equal(await page.locator('.message-row.is-evidence-target').getAttribute('data-direction'),'outgoing');assert(await page.locator('.message-row.is-evidence-target').evaluate(node=>node.classList.contains('own')));await navigate('overview');await id('ai-new-thread').click();await id('ai-scope').selectOption('all');await draft('今天有哪些重点？','analysis');assert.equal((await command({command:'counts'})).calls,0);
    });
    await check('cloud_config_explicit_local_save_key_cleared_no_automatic_test',async()=>{
      await configure({base:'https://api.minimax.cn/v1',model:'MiniMax-M3.1-Flash',key:'SYNTHETIC_KEY_ONLY_NOT_REAL'});
      assert.equal(await id('model-api-key').inputValue(),'');assert.equal(await id('model-api-key').getAttribute('placeholder'),'已配置 · 留空保留');assert.equal((await command({command:'counts'})).calls,0);assert.equal(await page.evaluate(()=>localStorage.length+sessionStorage.length),0);await shot('desktop-settings-1440');await navigate('overview');await draft('今天有哪些重点？','analysis');await id('ai-cloud-consent').waitFor();
    });
    await check('saved_cloud_target_confirmation_fits_988_and390_without_cloud_calls',async()=>{
      await page.setViewportSize({width:988,height:776});await noOverflow();await welcomeFits();await shot('desktop-ai-cloud-988');await page.setViewportSize({width:390,height:844});await noOverflow();await welcomeFits();await shot('mobile-ai-cloud');await page.setViewportSize({width:1440,height:1000});assert.equal((await command({command:'counts'})).calls,0);
    });
    await check('settings_navigation_clears_unsaved_key_and_preserves_saved_model',async()=>{
      await navigate('settings');assert.equal(await id('model-api-key').inputValue(),'');assert.equal(await id('model-base-url').inputValue(),'https://api.minimax.cn/v1');assert.equal(await id('model-name').inputValue(),'MiniMax-M3.1-Flash');await id('model-api-key').fill('SYNTHETIC_UNSAVED_KEY_NEVER_SENT');await id('model-delete-key').check();await navigate('overview');assert.equal(await id('model-api-key').inputValue(),'');assert.equal(await id('model-delete-key').isChecked(),false);await navigate('settings');assert.equal(await id('model-api-key').inputValue(),'');assert.equal(await id('model-base-url').inputValue(),'https://api.minimax.cn/v1');assert.equal(await id('model-name').inputValue(),'MiniMax-M3.1-Flash');assert.equal((await command({command:'counts'})).calls,0);await navigate('overview');
    });
    await check('visible_cloud_target_consent_required_and_reset_after_scope_or_time_change',async()=>{
      assert((await id('ai-cloud-confirm-label').innerText()).includes('api.minimax.cn'));const beforeRequests=questionRequests;await draft('合成报价','analysis');await id('ai-send').click();await page.waitForFunction(()=>document.querySelector('#ai-form-notice').textContent.includes('勾选'));assert.equal(questionRequests,beforeRequests);
      await id('ai-cloud-confirm').check();await id('ai-scope').selectOption('private_test');assert.equal(await id('ai-cloud-confirm').isChecked(),false);await draft('合成报价','analysis');await id('ai-cloud-confirm').check();await id('ai-time-range').selectOption('all');assert.equal(await id('ai-cloud-confirm').isChecked(),false);await draft('合成报价','analysis');await id('ai-cloud-confirm').check();await draft('今天有几条消息','local_stats');assert.equal(await id('ai-cloud-consent').isVisible(),false);await draft('合成报价','analysis');assert.equal(await id('ai-cloud-confirm').isChecked(),false);assert.equal((await command({command:'counts'})).calls,0);
    });
    await check('local_synthetic_provider_save_and_explicit_test_use_real_http_without_chat',async()=>{
      await configure({base:ready.synthetic_provider_url,model:'synthetic-model',remove:true});await id('model-test').click();await page.waitForFunction(()=>document.querySelector('#model-feedback').textContent.includes('测试通过'),null,{timeout:15000});const count=await command({command:'counts'});assert.equal(count.test_calls,1);assert.equal(count.question_calls,0);assert(await id('view-settings').isVisible());await navigate('overview');assert.equal(await id('ai-cloud-consent').isVisible(),false);assert.equal(await id('ai-form-notice').isVisible(),false);await id('ai-scope').selectOption('all');
    });
    await check('988px_ai_and_settings_page_fit_without_horizontal_overflow',async()=>{
      await page.setViewportSize({width:988,height:776});await noOverflow();await shot('desktop-ai-988');await navigate('settings');await noOverflow();assert.equal(await id('model-name').inputValue(),'synthetic-model');assert.equal(await id('model-api-key').inputValue(),'');await shot('desktop-settings-988');await navigate('overview');await page.setViewportSize({width:1440,height:1000});
    });
    await check('actual_retrieval_generation_answer_and_valid_citations',async()=>{
      await ask('合成报价有哪些约定？');await id('ai-runtime').waitFor();await page.waitForFunction(()=>document.querySelector('#ai-stage-text').textContent.includes('生成'),null,{timeout:15000});await idle();assert((await page.locator('.ai-assistant .ai-answer-text').last().innerText()).includes('合成供应商'));assert(await page.locator('.ai-evidence-card').count()>0);assert(await page.locator('.ai-inline-citation').count()>0);assert.equal((await command({command:'counts'})).question_calls,1);
    });await id('ai-chat-scroll').evaluate(node=>{node.scrollTop=0;});await shot('desktop-answer');
    await check('citation_outside_latest100_opens_exact_context_and_literal_source',async()=>{
      await page.locator('.ai-inline-citation').first().click();await id('view-messages').waitFor();await id('citation-context').waitFor();const target=page.locator('.message-row.is-evidence-target');await target.waitFor();assert.equal(await target.getAttribute('data-message-id'),'1');assert.equal(await target.getAttribute('data-direction'),'incoming');assert((await target.innerText()).includes('<img src=x'));assert.equal(await page.locator('.message-body img,.message-body script').count(),0);
      await command({command:'commit',body:'合成引用期间到达的新文字'});await page.waitForFunction(()=>document.querySelector('#total-messages').textContent==='127 条');assert.equal(await page.locator('.message-row.is-evidence-target').getAttribute('data-message-id'),'1');assert.equal(await page.locator('.message-body').filter({hasText:'合成引用期间到达的新文字'}).count(),0);await id('citation-latest').click();await page.getByText('合成引用期间到达的新文字',{exact:true}).first().waitFor();await navigate('overview');
    });
    await check('multiple_turns_same_thread_history_and_scope_change_new_context',async()=>{
      await ask('这个报价还需要确认什么？');await idle();assert.equal(await page.locator('.ai-user').count(),2);assert.equal(await page.locator('.ai-assistant').count(),2);assert(await page.locator('.ai-history-item').count()>=1);
      await id('ai-scope').selectOption('group_test');assert.equal(await page.locator('.ai-turn').count(),0);await ask('群内交付约定是什么？');await idle();for(const text of await page.locator('.ai-evidence-top strong').allTextContents())assert(text.includes('合成订单测试群'));
    });
    await check('history_reload_restores_original_scope_and_multiple_turns',async()=>{
      await page.locator('.ai-history-item').filter({hasText:'合成报价有哪些约定？'}).click();await page.waitForFunction(()=>document.querySelectorAll('.ai-user').length===2);assert.equal(await id('ai-scope').inputValue(),'all');assert.equal(await id('ai-time-range').inputValue(),'all');assert.equal(await page.locator('.ai-assistant').count(),2);
    });
    await check('mixed_request_combines_exact_full_count_with_bounded_model_evidence',async()=>{
      const calls=(await command({command:'counts'})).question_calls;await id('ai-new-thread').click();await id('ai-scope').selectOption('all');await draft('统计今天各会话消息数量并分析重点','mixed');await id('ai-send').click();await idle();const answer=await savedAnswer('统计今天各会话消息数量并分析重点');assert.equal(answer.plan.route,'mixed');assert.equal(answer.statistics.message_count,127);assert.equal(answer.statistics.group_by,'conversation');assert.equal(answer.statistics.rows.length,2);assert.deepEqual(answer.statistics.rows.map(row=>row.message_count).sort((a,b)=>a-b),[1,126]);assert(answer.evidence.length<=30);assert(answer.content.includes('127'));assert(answer.content.includes('126'));assert(answer.content.includes('合成供应商'));assert((await page.locator('.ai-route-label').last().innerText()).includes('统计与分析'));assert.equal((await command({command:'counts'})).question_calls,calls+1);await shot('desktop-mixed-answer');
    });
    await check('outgoing_analysis_retrieves_only_verified_self_text_and_keeps_direction',async()=>{
      const calls=(await command({command:'counts'})).question_calls;await id('ai-new-thread').click();await ask('我发出的消息有什么约定？');await idle();const answer=await savedAnswer('我发出的消息有什么约定？');assert.equal(answer.plan.route,'analysis');assert.equal(answer.plan.direction,'outgoing');assert(answer.evidence.length>0);assert(answer.evidence.every(row=>row.direction==='outgoing'&&row.own===true&&row.sender==='我'));assert(answer.evidence.every(row=>row.message_id===125));assert.equal((await command({command:'counts'})).question_calls,calls+1);await page.locator('.ai-inline-citation').first().click();await id('citation-context').waitFor();assert.equal(await page.locator('.message-row.is-evidence-target').getAttribute('data-direction'),'outgoing');await navigate('overview');
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
      await id('ai-knowledge-toggle').click();await id('knowledge-dialog').waitFor();assert.equal(await id('ai-kb-conversations').innerText(),'2 个');assert.equal(await id('ai-kb-messages').innerText(),'127 条');assert.equal(await id('ai-kb-identity').innerText(),'已自动核验');assert.notEqual(await id('ai-kb-baseline').innerText(),'暂无记录');assert((await id('knowledge-dialog').innerText()).includes('不代表完整微信历史'));await id('close-knowledge').click();
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
      await navigate('settings');await id('pref-density').selectOption('compact');await id('save-settings').click();assert(await id('view-settings').isVisible());assert(await page.locator('body').evaluate(node=>node.classList.contains('compact')));await command({command:'status',stage:'stopped'});await navigate('collection');await page.waitForFunction(()=>document.querySelector('#collection-stage-title').textContent==='采集已停止');assert.equal(await id('collection-toggle').isVisible(),false);
    });
    await check('390px_ai_drawer_settings_page_and_message_context_fit',async()=>{
      await page.setViewportSize({width:390,height:844});await navigate('overview');await id('ai-new-thread').click();await noOverflow();await id('toast').waitFor({state:'hidden'});await shot('mobile-ai-empty');await navigate('settings');await noOverflow();assert(await id('model-form').isVisible());assert(await id('settings-form').isVisible());assert.equal(await id('model-api-key').inputValue(),'');assert.equal(await id('model-name').inputValue(),'synthetic-model');await shot('mobile-settings');await navigate('overview');await id('nav-toggle').click();assert.equal(Math.round((await id('sidebar').boundingBox()).width),245);await page.keyboard.press('Escape');assert.equal(await id('nav-toggle').getAttribute('aria-expanded'),'false');await navigate('messages');await id('mobile-back').click();await page.locator('[data-testid=conversation-item-group_test]').click();await noOverflow();await shot('mobile-messages');await id('mobile-back').click();await page.locator('[data-testid=conversation-item-private_test]').click();await page.locator('.message-row[data-message-id="125"]').waitFor();assert.equal(await page.locator('.message-row[data-message-id="125"]').getAttribute('data-direction'),'outgoing');assert.equal(await page.locator('.message-row[data-message-id="124"]').getAttribute('data-direction'),'unknown');await noOverflow();await shot('mobile-message-directions');
    });
    await check('file_preview_ai_is_explicit_fiction_and_keeps_message_controls',async()=>{
      const preview=await context.newPage();let apiCalls=0;preview.on('request',request=>{if(new URL(request.url()).pathname.startsWith('/api/'))apiCalls++;});preview.on('pageerror',()=>issues.page++);await preview.goto('file://'+path.join(root,'apps/web/index.html'));await preview.locator('[data-ai-prompt=agreement]').click();await preview.locator('#ai-send').click();await preview.getByText('ChatLens · 虚构回答示例',{exact:true}).waitFor();await preview.waitForFunction(()=>document.querySelectorAll('.ai-inline-citation').length===2);assert((await preview.locator('#ai-turns').innerText()).includes('虚构回答示例'));await preview.locator('.ai-inline-citation').first().click();await preview.locator('.message-row.is-evidence-target').waitFor();assert(await preview.locator('#simulate-message').isVisible());assert.equal(apiCalls,0);await preview.close();
    });
    assert.equal(issues.page,0);assert.equal(issues.external,0);assert.deepEqual(hashes(),before);
    const counts=await command({command:'counts'});
    assert.equal(counts.network_guard.blocked_attempts,1,'Only the explicit guard self-check may attempt an external connection.');
    assert.equal(counts.raw_identity_leaks,0,'Raw account/sender identity must not be sent to the synthetic provider.');
    const result={status:'passed',scope:'actual_ai_routing_statistics_assistant_collector_http_and_explicit_synthetic_loopback_provider',viewports:['1440x1000','988x776','390x844'],settings_surface:'dedicated_page',real_minimax_or_wechat_used:false,checks,passed_check_count:checks.length,source_sha256:before,browser_runtime_errors:issues.page,browser_external_requests:issues.external,local_plan_requests:planRequests,network_guard:counts.network_guard,synthetic_provider_raw_identity_leaks:counts.raw_identity_leaks,synthetic_provider_calls:counts.calls,synthetic_provider_test_calls:counts.test_calls,synthetic_provider_question_calls:counts.question_calls};
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
