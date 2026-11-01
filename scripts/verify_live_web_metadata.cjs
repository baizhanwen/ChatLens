#!/usr/bin/env node
'use strict';
// Read-only browser observation of an already-running local Web workspace.
// The private launch URL is read in memory only. No chat text, names, message
// IDs, credentials, screenshots, traces, videos, or browser logs are exported.
const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');

const root = path.resolve(__dirname, '..');
const args = process.argv.slice(2);
const option = (name, fallback) => args.includes(name) ? args[args.indexOf(name) + 1] : fallback;
const fail = code => { const error = new Error(); error.safeCode = code; throw error; };
const sourceNames = ['apps/web/index.html', 'apps/web/app.js', 'apps/web/styles.css'];
const hashes = () => sourceNames.map(name => createHash('sha256').update(fs.readFileSync(path.join(root, name))).digest('hex')).join(':');

function options() {
  const input = option('--session-file');
  const output = option('--output');
  const observe = Number(option('--observe-ms', '15000'));
  const expected = Number(option('--expected-conversations', '2'));
  if (!input || !output || !Number.isInteger(observe) || observe < 0 || observe > 60000 || !Number.isInteger(expected) || expected < 1 || expected > 20) fail('verification_arguments_invalid');
  const target = path.resolve(output);
  if (fs.existsSync(target)) fail('verification_output_already_exists');
  return { input: path.resolve(input), output: target, observe, expected };
}

function privateSession(input) {
  const info = fs.lstatSync(input);
  if (!info.isFile() || info.isSymbolicLink() || info.nlink !== 1 || info.size > 16384 || (process.getuid && (info.uid !== process.getuid() || (info.mode & 0o077)))) fail('verification_session_file_unsafe');
  const raw = JSON.parse(fs.readFileSync(input, 'utf8'));
  if (!raw || typeof raw.launch_url !== 'string') fail('verification_session_format_invalid');
  const url = new URL(raw.launch_url);
  if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || !url.port || !['/', '/index.html'].includes(url.pathname) || url.username || url.password || url.search) fail('verification_session_origin_invalid');
  const token = new URLSearchParams(url.hash.slice(1)).get('token');
  if (!token || !/^[A-Za-z0-9_-]{32,128}$/.test(token)) fail('verification_session_format_invalid');
  return { launch: url.href, origin: url.origin };
}

async function main() {
  let browser, config, stage = 'verification_arguments_invalid';
  const observed = { page_errors: 0, console_error_count: 0, external_requests: 0, api_requests: 0, api_errors: 0, navigation_errors: 0 };
  const summary = { schema_version: 1, scope: 'existing_local_web_dom_metadata_only', recorded_at_utc: new Date().toISOString(), status: '未验证', collector_started_by_verifier: false, source_database_opened_by_verifier: false, chat_text_exported: false, personal_names_exported: false, message_ids_exported: false, session_credentials_exported: false, screenshots_or_traces_saved: false };
  try {
    config = options();
    stage = 'verification_session_file_unavailable';
    let session = privateSession(config.input);
    const origin = session.origin;
    const sourceBefore = hashes();
    stage = 'verification_browser_unavailable';
    const { chromium } = require(process.env.CHATLENS_PLAYWRIGHT_MODULE || 'playwright');
    const launch = { headless: true };
    if (process.env.CHATLENS_BROWSER_EXECUTABLE) launch.executablePath = process.env.CHATLENS_BROWSER_EXECUTABLE;
    browser = await chromium.launch(launch);
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: 'reduce', acceptDownloads: false });
    await context.route('**/*', route => {
      let url;
      try { url = new URL(route.request().url()); } catch { observed.external_requests++; return route.abort(); }
      if (url.origin !== origin || url.protocol !== 'http:') { observed.external_requests++; return route.abort(); }
      return route.continue();
    });
    const page = await context.newPage();
    page.on('pageerror', () => { observed.page_errors++; });
    page.on('console', message => { if (message.type() === 'error') observed.console_error_count++; });
    page.on('dialog', dialog => { observed.page_errors++; dialog.dismiss().catch(() => {}); });
    page.on('download', download => { observed.external_requests++; download.cancel().catch(() => {}); });
    context.on('page', other => { if (other !== page) { observed.external_requests++; other.close().catch(() => {}); } });
    page.on('response', response => {
      let url;
      try { url = new URL(response.url()); } catch { return; }
      if (url.origin === origin && url.pathname.startsWith('/api/')) {
        observed.api_requests++;
        if (response.status() >= 400) observed.api_errors++;
      }
    });
    page.on('requestfailed', request => { if (request.isNavigationRequest()) observed.navigation_errors++; });
    stage = 'verification_http_navigation_failed';
    await page.goto(session.launch, { waitUntil: 'domcontentloaded', timeout: 15000 });
    session = null;
    stage = 'verification_live_bootstrap_failed';
    await page.waitForFunction(() => document.body.classList.contains('live-mode') && document.querySelectorAll('.conversation-item').length > 0 && document.querySelector('#live-connection-notice')?.hidden === true, null, { timeout: 15000 });
    summary.fragment_removed = new URL(page.url()).hash === '';
    summary.persistent_storage_entries = await page.evaluate(() => localStorage.length + sessionStorage.length);
    summary.http_only_session_cookie = (await context.cookies()).some(cookie => cookie.name === 'chatlens_session' && cookie.httpOnly && cookie.sameSite === 'Strict');
    summary.conversation_count = await page.locator('.conversation-item').count();
    if (summary.conversation_count !== config.expected) fail('verification_conversation_count_mismatch');
    await page.locator('#nav-messages').click();

    async function inspect(index, reset = false) {
      const item = page.locator('.conversation-item').nth(index);
      const alias = (await item.getAttribute('data-testid')).slice('conversation-item-'.length);
      const committedResponse = page.waitForResponse(response => {
        const url = new URL(response.url());
        return url.origin === origin && url.pathname === '/api/messages' && url.searchParams.get('conversation') === alias && response.status() === 200;
      }, { timeout: 12000 });
      await item.click();
      await committedResponse;
      await page.waitForTimeout(100);
      await page.waitForFunction(() => {
        const loading = document.querySelector('#message-list .empty-state strong')?.textContent === '正在读取消息';
        return Boolean(document.querySelector('#thread-title')?.textContent) && !loading;
      }, null, { timeout: 12000 });
      // Return fixed UI labels, booleans and counts only. Any message IDs used
      // for duplicate/new-row comparison stay inside this ephemeral browser.
      return page.evaluate(({ index, reset }) => {
        const labels = { '正在初始化': 'starting', '正在采集': 'collecting', '正在停止采集': 'stopping', '采集已暂停': 'paused', '采集已停止': 'stopped', '采集异常': 'error', '采集心跳已过期': 'stale', '采集状态不可用': 'unavailable', '页面连接中断': 'connection_error', '本机会话已失效': 'authentication_expired', '正在连接本机服务': 'connecting' };
        const nodes = [...document.querySelectorAll('.message-row')];
        const ids = nodes.map(node => Number(node.dataset.messageId));
        const valid = ids.every(value => Number.isSafeInteger(value) && value > 0);
        const rowMax = ids.length ? Math.max(...ids) : 0;
        window.__chatlensMetadataProbe ||= [];
        let prior = window.__chatlensMetadataProbe[index];
        if (reset || !prior) prior = { maximum: rowMax, added: 0 };
        else prior.added += ids.filter(value => value > prior.maximum).length;
        prior.maximum = Math.max(prior.maximum, rowMax);
        window.__chatlensMetadataProbe[index] = prior;
        const numeric = selector => {
          const text = document.querySelector(selector)?.textContent || '';
          const match = text.match(/^\s*(\d+)/);
          return match ? Number(match[1]) : null;
        };
        return {
          rendered_rows: nodes.length, saved_count: numeric('#message-count'), global_saved_count: numeric('#total-messages'),
          new_rendered_rows: prior.added, duplicate_rows: valid ? ids.length - new Set(ids).size : nodes.length,
          row_ids_valid: valid, header_present: Boolean(document.querySelector('#thread-title')?.textContent),
          status: labels[document.querySelector('#ribbon-state')?.textContent] || 'unknown',
          connection_notice_visible: !document.querySelector('#live-connection-notice')?.hidden,
          embedded_message_elements: document.querySelectorAll('.message-body img,.message-body script,.message-body iframe,.message-body object,.message-body embed').length,
          type: document.querySelector('#thread-type')?.textContent === '群聊' ? 'group' : 'private',
          horizontal_overflow: document.documentElement.scrollWidth > innerWidth + 1
        };
      }, { index, reset });
    }

    stage = 'verification_dom_observation_failed';
    const baseline = [];
    const latest = [];
    const statuses = new Set();
    for (let index = 0; index < summary.conversation_count; index++) {
      baseline[index] = await inspect(index, true);
      latest[index] = baseline[index];
      statuses.add(baseline[index].status);
    }
    if (args.includes('--announce-baseline')) process.stdout.write(JSON.stringify({ event: 'metadata_baseline_ready', conversation_count: summary.conversation_count, saved_counts: baseline.map(row => row.saved_count) }) + '\n');
    const start = Date.now();
    while (Date.now() - start < config.observe) {
      await page.waitForTimeout(Math.max(0, Math.min(1000, config.observe - (Date.now() - start))));
      for (let index = 0; index < summary.conversation_count; index++) {
        latest[index] = await inspect(index);
        statuses.add(latest[index].status);
      }
    }
    summary.observation_ms = Date.now() - start;
    summary.baseline_rendered_rows = baseline.map(row => row.rendered_rows);
    summary.final_rendered_rows = latest.map(row => row.rendered_rows);
    summary.baseline_saved_counts = baseline.map(row => row.saved_count);
    summary.final_saved_counts = latest.map(row => row.saved_count);
    summary.saved_count_increments = latest.map((row, index) => Math.max(0, (row.saved_count || 0) - (baseline[index].saved_count || 0)));
    summary.new_rendered_rows = latest.map(row => row.new_rendered_rows);
    summary.new_messages_observed = latest.some(row => row.new_rendered_rows > 0);
    summary.new_dom_row_observation = summary.new_messages_observed ? '通过' : '未验证';
    summary.collection_running_observed = statuses.has('collecting');
    summary.observed_states = [...statuses].sort();
    summary.final_state = latest[0]?.status || 'unknown';
    summary.all_headers_present = latest.every(row => row.header_present);
    summary.all_row_ids_valid = latest.every(row => row.row_ids_valid);
    summary.duplicate_row_count = latest.reduce((sum, row) => sum + row.duplicate_rows, 0);
    summary.embedded_message_element_count = latest.reduce((sum, row) => sum + row.embedded_message_elements, 0);
    summary.connection_notice_visible = latest.some(row => row.connection_notice_visible);
    summary.horizontal_overflow = latest.some(row => row.horizontal_overflow);
    summary.frontend_sources_unchanged = hashes() === sourceBefore;
    Object.assign(summary, observed);
    const passed = summary.fragment_removed && summary.http_only_session_cookie && summary.persistent_storage_entries === 0 && summary.all_headers_present && summary.all_row_ids_valid && !summary.duplicate_row_count && !summary.embedded_message_element_count && !summary.connection_notice_visible && !summary.horizontal_overflow && summary.frontend_sources_unchanged && !observed.page_errors && !observed.external_requests && !observed.api_errors;
    summary.status = passed ? '通过' : '失败';
    summary.reason = passed ? null : 'verification_metadata_checks_failed';
  } catch (error) {
    summary.status = '失败';
    summary.reason = typeof error.safeCode === 'string' ? error.safeCode : stage;
    Object.assign(summary, observed);
  } finally {
    if (browser) await browser.close().catch(() => {});
  }
  if (config) {
    fs.mkdirSync(path.dirname(config.output), { recursive: true });
    fs.writeFileSync(config.output, JSON.stringify(summary, null, 2) + '\n', { flag: 'wx' });
  }
  process.stdout.write(JSON.stringify(summary) + '\n');
  if (summary.status !== '通过') process.exitCode = 1;
}
main().catch(() => { process.stdout.write('{"status":"失败","reason":"verification_report_write_failed"}\n'); process.exitCode = 1; });
