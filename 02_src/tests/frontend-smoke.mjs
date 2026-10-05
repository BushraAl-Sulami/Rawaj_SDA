/**
 * Real-browser integration checks against an isolated Rawaj server.
 * Usage: node tests/frontend-smoke.mjs http://127.0.0.1:18766
 * Requires installed Chrome; uses native Node CDP, no automation dependency.
 * The server must use a disposable database. Paid AI requests are intercepted.
 */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {mkdtemp, mkdir, readFile, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const baseUrl = (process.argv[2] || 'http://127.0.0.1:18766').replace(/\/$/, '');
const browserPath = process.env.RAWAJ_TEST_CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const outputDir = join(root, 'outputs');
const delay = milliseconds => new Promise(resolveDelay => setTimeout(resolveDelay, milliseconds));
const checks = [];
const runtimeErrors = [];
const networkErrors = [];
const ideaRequests = [];
const blockedPaidRequests = [];
const profileDir = await mkdtemp(join(tmpdir(), 'rawaj-browser-test-'));
let socket;
let browser;
let restaurantId;
let expectedTask;
let failNextIdeas = false;
let messageId = 0;
const pending = new Map();

async function until(operation, description, timeout = 15000) {
  const deadline = Date.now() + timeout;
  let lastError;
  while (Date.now() < deadline) {
    try {
      const result = await operation();
      if (result) return result;
    } catch (error) { lastError = error; }
    await delay(80);
  }
  throw new Error(`Timed out: ${description}${lastError ? ` (${lastError.message})` : ''}`);
}

function command(method, params = {}) {
  return new Promise((resolveCommand, rejectCommand) => {
    const id = ++messageId;
    const timer = setTimeout(() => {
      pending.delete(id);
      rejectCommand(new Error(`CDP timeout: ${method}`));
    }, 12000);
    pending.set(id, {resolve: resolveCommand, reject: rejectCommand, timer});
    socket.send(JSON.stringify({id, method, params}));
  });
}

async function evaluate(expression) {
  const result = await command('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true});
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text + ': ' + (result.exceptionDetails.exception?.description || expression));
  return result.result.value;
}

async function api(path, options = {}) {
  const response = await fetch(`${baseUrl}${path}`, options);
  const payload = await response.json();
  if (!response.ok) throw new Error(`${options.method || 'GET'} ${path}: ${response.status} ${JSON.stringify(payload)}`);
  return payload;
}

async function intercept(event) {
  const {requestId, request} = event;
  const url = new URL(request.url);
  try {
    const match = url.pathname.match(/^\/api\/restaurants\/(\d+)\/content-ideas$/);
    if (match) {
      const body = JSON.parse(request.postData || '{}');
      ideaRequests.push({restaurantId: Number(match[1]), ...body});
      if (failNextIdeas) {
        failNextIdeas = false;
        await command('Fetch.fulfillRequest', {requestId, responseCode: 502, responseHeaders: [{name: 'Content-Type', value: 'application/json'}], body: Buffer.from(JSON.stringify({detail: 'Test provider unavailable. Please retry.'})).toString('base64')});
        return;
      }
      const plan = await api(`/api/restaurants/${match[1]}/strategy?month=${body.month}`);
      const task = [...plan.tasks, ...(plan.occasions || [])].find(item => item.id === body.task_id);
      assert.ok(task, 'Idea request must reference a task in the saved current plan');
      expectedTask = task;
      const ideas = [1, 2, 3].map(id => ({
        id, name: `${plan.restaurant_name} creative direction ${id}`, label: `Direction ${id}`,
        description: `Feature the signature experience at ${plan.restaurant_name}.`,
        angle: `Signature story ${id}`, effort: 'Low', hook: `A closer look at ${plan.restaurant_name}`,
        content_format: task.type, why_it_fits: `Matches this ${plan.business_type} and the selected ${task.type} task.`,
      }));
      await command('Fetch.fulfillRequest', {requestId, responseCode: 200, responseHeaders: [{name: 'Content-Type', value: 'application/json'}], body: Buffer.from(JSON.stringify({ideas})).toString('base64')});
      return;
    }
    if (request.method === 'POST' && /\/analyze$|\/outreach\/draft$|\/api\/content-ideas$/.test(url.pathname)) {
      blockedPaidRequests.push(url.pathname);
      await command('Fetch.fulfillRequest', {requestId, responseCode: 503, responseHeaders: [{name: 'Content-Type', value: 'application/json'}], body: Buffer.from(JSON.stringify({detail: 'Paid calls are disabled during browser tests.'})).toString('base64')});
      return;
    }
    await command('Fetch.continueRequest', {requestId});
  } catch (error) {
    runtimeErrors.push(`Request interception: ${error.message}`);
    await command('Fetch.failRequest', {requestId, errorReason: 'Failed'}).catch(() => {});
  }
}

async function screenshot(name, mobile = false) {
  await command('Emulation.setDeviceMetricsOverride', mobile
    ? {width: 390, height: 844, deviceScaleFactor: 1, mobile: true}
    : {width: 1440, height: 1050, deviceScaleFactor: 1, mobile: false});
  await delay(150);
  await evaluate('window.scrollTo(0, 0)');
  const capture = await command('Page.captureScreenshot', {format: 'png', captureBeyondViewport: false});
  await writeFile(join(outputDir, name), Buffer.from(capture.data, 'base64'));
}

async function setField(label, value) {
  const success = await evaluate(`(() => {
    const labelText = ${JSON.stringify(label)};
    const labelElement = [...document.querySelectorAll('label')].find(node => node.textContent.trim().toLowerCase().startsWith(labelText.toLowerCase()));
    const field = labelElement?.control || labelElement?.querySelector('input,select,textarea') || [...document.querySelectorAll('input,select,textarea')].find(node => node.getAttribute('aria-label') === labelText || node.placeholder === labelText);
    if (!field) return false;
    let value = ${JSON.stringify(value)};
    if (field.tagName === 'SELECT') value = [...field.options].find(option => option.value === value || option.textContent.trim().toLowerCase() === value.toLowerCase())?.value ?? value;
    const prototype = field.tagName === 'SELECT' ? HTMLSelectElement.prototype : field.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, 'value').set.call(field, value);
    field.dispatchEvent(new Event('input', {bubbles:true}));
    field.dispatchEvent(new Event('change', {bubbles:true}));
    return true;
  })()`);
  assert.ok(success, `Field exists: ${label}`);
}

async function clickText(text, scope = 'document') {
  const result = await evaluate(`(() => {
    const scope = ${scope};
    const target = [...scope.querySelectorAll('button,a,[role="button"]')].find(node => node.textContent.trim().replace(/\\s+/g, ' ').toLowerCase() === ${JSON.stringify(text.toLowerCase())});
    if (!target || target.disabled) return false;
    target.scrollIntoView({block:'center'});
    target.click();
    return true;
  })()`);
  assert.ok(result, `Clickable control exists: ${text}`);
}

async function clickMatching(pattern) {
  const result = await evaluate(`(() => {
    const target = [...document.querySelectorAll('button,a,[role="button"]')].find(node => new RegExp(${JSON.stringify(pattern)}, 'i').test(node.textContent.trim().replace(/\\s+/g,' ')) && !node.disabled);
    if (!target) return false;
    target.scrollIntoView({block:'center'}); target.click(); return true;
  })()`);
  assert.ok(result, `Clickable control matches: ${pattern}`);
}

async function route(page) {
  await evaluate(`location.hash = '#/${page}'`);
  await until(() => evaluate(`location.hash === '#/${page}'`), `route ${page}`);
  await delay(120);
}

function passed(description) { checks.push(description); console.log(`PASS ${description}`); }

try {
  await mkdir(outputDir, {recursive: true});
  browser = spawn(browserPath, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-background-networking', '--disable-sync', '--disable-extensions',
    '--remote-debugging-port=0', `--user-data-dir=${profileDir}`, '--window-size=1440,1050', 'about:blank',
  ], {windowsHide: true, stdio: ['ignore', 'ignore', 'pipe']});
  let launchError;
  browser.on('error', error => { launchError = error; });
  browser.stderr.resume();
  const port = await until(async () => {
    if (launchError) throw launchError;
    return (await readFile(join(profileDir, 'DevToolsActivePort'), 'utf8')).split('\n')[0];
  }, 'Chrome remote debugging', 12000);
  const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  socket = new WebSocket(targets.find(target => target.type === 'page').webSocketDebuggerUrl);
  await new Promise((resolveOpen, rejectOpen) => { socket.addEventListener('open', resolveOpen, {once: true}); socket.addEventListener('error', rejectOpen, {once: true}); });
  socket.addEventListener('message', event => {
    const message = JSON.parse(event.data);
    if (message.id) {
      const request = pending.get(message.id);
      if (!request) return;
      clearTimeout(request.timer);
      pending.delete(message.id);
      if (message.error) request.reject(new Error(`${message.error.code}: ${message.error.message}`));
      else request.resolve(message.result);
    } else if (message.method === 'Fetch.requestPaused') void intercept(message.params);
    else if (message.method === 'Runtime.exceptionThrown') runtimeErrors.push(message.params.exceptionDetails.exception?.description || message.params.exceptionDetails.text);
    else if (message.method === 'Network.responseReceived' && message.params.response.status >= 400) {
      const response = message.params.response;
      networkErrors.push({path: new URL(response.url).pathname, status: response.status});
    }
  });
  await command('Page.enable');
  await command('Runtime.enable');
  await command('Network.enable');
  await command('Fetch.enable', {patterns: [{urlPattern: '*api*', requestStage: 'Request'}]});
  await command('Emulation.setDeviceMetricsOverride', {width: 1440, height: 1050, deviceScaleFactor: 1, mobile: false});
  await command('Page.navigate', {url: `${baseUrl}/#/strategy`});
  await until(() => evaluate(`Boolean(document.querySelector('input[type="password"]'))`), 'unauthenticated sign-in');
  assert.equal(await evaluate('location.hash'), '#/sign-in');
  await screenshot('frontend-sign-in.png');
  await setField('Email', 'demo@example.com');
  await setField('Password', 'rawajdemo');
  await clickText('Sign in');
  await until(() => evaluate(`location.hash === '#/home' && document.querySelector('aside nav') !== null`), 'sign-in opens Home');
  const navigation = await evaluate(`[...document.querySelectorAll('aside nav a,aside nav button')].map(node=>node.textContent.trim().replace(/\\s+/g,' '))`);
  assert.deepEqual(navigation, ['Home', 'Monthly Strategy', 'Content Creation']);
  const stored = await evaluate(`JSON.stringify({session: {...sessionStorage}, local: {...localStorage}})`);
  assert.ok(!stored.includes('rawajdemo'), 'Password is not stored in web storage');
  passed('Sign-in opens Home, password stays out of storage, and sidebar has exactly 3 pages');

  await until(() => evaluate(`Boolean([...document.querySelectorAll('label')].find(node=>node.textContent.startsWith('Restaurant name')))`), 'Home context form');
  if (await evaluate(`[...document.querySelectorAll('button')].some(node=>node.textContent.trim()==='Add restaurant')`)) {
    await clickText('Add restaurant');
    await until(() => evaluate(`!document.querySelector('input[name="instagram_username"]').disabled`), 'new restaurant form');
  }
  const handle = `rawaj_smoke_${Date.now().toString(36)}`;
  for (const [label, value] of [
    ['Restaurant name', 'Palm & Brew Café'], ['Instagram username', handle], ['Business type', 'cafe'],
    ['Cuisine', 'Specialty coffee and fresh pastries'], ['Location', 'Riyadh'], ['Contact email', 'cafe@example.com'],
    ['Target audience', 'Students and remote workers'], ['Signature items', 'Pistachio latte, croissants'],
    ['Marketing goals', 'Increase weekday café visits'], ['Brand tone', 'Warm and playful'],
  ]) await setField(label, value);
  await clickMatching('^(Create restaurant|Save (restaurant|context))');
  const created = await until(async () => (await api('/api/restaurants?limit=100')).items.find(item=>item.instagram_username===handle), 'restaurant saved through Home');
  restaurantId = created.id;
  assert.equal(created.name, 'Palm & Brew Café');
  assert.equal(created.context.business_type, 'cafe');
  await until(() => evaluate(`document.querySelector('aside').textContent.includes('Palm & Brew Café')`), 'sidebar context updates');
  await screenshot('frontend-home.png');
  passed('Home saves café profile and restaurant context through FastAPI');

  await clickText('Monthly Strategy', `document.querySelector('aside nav')`);
  await until(() => evaluate(`location.hash === '#/strategy' && document.body.textContent.includes('Palm & Brew Café') && document.querySelector('.calendar') !== null`), 'monthly strategy and calendar');
  const month = await evaluate(`document.querySelector('input[type="month"]')?.value || ''`);
  assert.match(month, /^\d{4}-\d{2}$/);
  const plan = await until(() => api(`/api/restaurants/${restaurantId}/strategy?month=${month}`), 'stored strategy');
  assert.equal(plan.business_type, 'cafe');
  assert.equal(plan.restaurant_name, 'Palm & Brew Café');
  assert.ok(plan.summary.includes('Palm & Brew Café'));
  assert.equal(plan.goal, 'Increase weekday café visits');
  assert.ok(plan.tasks.length > 0);
  await screenshot('frontend-strategy.png');
  passed('Monthly strategy is loaded from the current restaurant and saved calendar plan');

  // Additional calendar and content selectors are semantic and validated against
  // the current UI below. A complete month check includes weekday alignment.
  const [year, monthNumber] = month.split('-').map(Number);
  const leadingDays = (new Date(year, monthNumber-1, 1).getDay()+6)%7;
  const calendar = await evaluate(`(() => {const days=document.querySelector('.days');return days ? [...days.children].map(node=>({text:node.querySelector('span')?.textContent.trim()||node.textContent.trim(),blank:node.classList.contains('blank')||node.classList.contains('emptyDay')||node.getAttribute('aria-hidden')==='true'})) : [];})()`);
  assert.ok(calendar.length >= new Date(year, monthNumber, 0).getDate()+leadingDays, 'Calendar includes weekday offset');
  assert.equal(calendar[leadingDays].text, '1', 'First day is under the correct weekday');
  passed('Calendar grid includes the correct Monday-first weekday offset');

  await setField('Planning month', '2028-02');
  await until(() => evaluate(`document.querySelectorAll('.days button[data-date^="2028-02-"]').length === 29`), 'leap-year February calendar');
  assert.equal(await evaluate(`document.querySelector('.days').children[1].getAttribute('data-date')`), '2028-02-01');
  const leapPlan = await api(`/api/restaurants/${restaurantId}/strategy?month=2028-02`);
  assert.ok(leapPlan.tasks.every(task=>task.date.startsWith('2028-02-')));
  await setField('Planning month', month);
  await until(() => evaluate(`Boolean(document.querySelector('.days button[data-date^="${month}-"]'))`), 'restore original month');
  passed('Changing planning month renders February 2028 with 29 days and the correct weekday');

  await clickText('Content Creation', `document.querySelector('aside nav')`);
  await until(() => evaluate(`location.hash === '#/content' && document.body.textContent.includes('Palm & Brew Café')`), 'Content Creation');
  failNextIdeas = true;
  await clickMatching('^(Generate (content |fresh )?ideas|Get content ideas)$');
  await until(() => evaluate(`Boolean(document.querySelector('.contentError[role="alert"]'))`), 'provider error is visible');
  assert.equal(await evaluate(`document.querySelectorAll('.ideaCard').length`), 0);
  await clickText('Try again');
  await until(() => evaluate(`document.body.textContent.includes('creative direction 1')`), 'contextual content ideas');
  passed('Content generation failure is visible and retry succeeds without losing the selected task');
  assert.equal(ideaRequests.at(-1).restaurantId, restaurantId);
  assert.equal(ideaRequests.at(-1).month, month);
  assert.ok(expectedTask);
  await clickMatching('Palm & Brew Café creative direction 1');
  await clickMatching('^(Use this idea|Save (this )?idea|Save to calendar)$');
  await until(async () => (await api(`/api/restaurants/${restaurantId}/strategy?month=${month}`)).tasks.find(item=>item.id===expectedTask.id)?.saved_idea?.name.includes('creative direction 1'), 'idea saved to backend task');
  await screenshot('frontend-content.png');
  await command('Page.reload');
  await until(() => evaluate(`document.body.textContent.includes('creative direction 1')`), 'saved idea survives reload');
  await setField('Calendar task', plan.tasks[1].id);
  await until(() => evaluate(`!document.querySelector('.savedContentCard') && !document.querySelector('.ideaCard')`), 'switching tasks clears previous ideas');
  await setField('Calendar task', expectedTask.id);
  await until(() => evaluate(`document.querySelector('.savedContentCard')?.textContent.includes('creative direction 1')`), 'saved idea returns only for the matching task');
  passed('Content ideas use selected restaurant/task and chosen idea persists through reload');

  await screenshot('frontend-mobile.png', true);
  const overflow = await evaluate('document.documentElement.scrollWidth > window.innerWidth + 1');
  assert.equal(overflow, false, 'Mobile layout has no horizontal page overflow');
  assert.equal(await evaluate(`[...document.querySelectorAll('aside nav a')].filter(node=>node.getBoundingClientRect().width>0).length`), 3, 'All three navigation links remain visible on mobile');
  await command('Emulation.setDeviceMetricsOverride', {width:1440,height:1050,deviceScaleFactor:1,mobile:false});
  passed('Mobile content view fits a 390px viewport and keeps all three navigation links');

  await clickText('Home', `document.querySelector('aside nav')`);
  await until(() => evaluate(`Boolean([...document.querySelectorAll('label')].find(node=>node.textContent.startsWith('Restaurant name')))`), 'Home editing form');
  for (const [label, value] of [
    ['Restaurant name', 'Saffron Table'], ['Business type', 'restaurant'], ['Cuisine', 'Saudi cuisine'],
    ['Signature items', 'Kabsa, grilled chicken'], ['Target audience', 'Families'],
    ['Marketing goals', 'Increase family dinner bookings'],
  ]) await setField(label,value);
  await clickMatching('^Save (restaurant|context)');
  await until(async () => (await api(`/api/restaurants/${restaurantId}`)).name === 'Saffron Table', 'changed context saved');
  await clickText('Monthly Strategy', `document.querySelector('aside nav')`);
  const updated = await until(async () => {
    const candidate = await api(`/api/restaurants/${restaurantId}/strategy?month=${month}`);
    return candidate.restaurant_name === 'Saffron Table' && candidate.business_type === 'restaurant' ? candidate : null;
  }, 'strategy regenerated after context update');
  assert.notEqual(updated.id, plan.id, 'New context gets a new plan revision');
  assert.ok(updated.tasks.every(item=>!item.saved_idea), 'Old restaurant ideas are not copied into a new context');
  assert.ok(!(await evaluate(`document.querySelector('.strategyPage').textContent`)).includes('Palm & Brew Café'), 'Old restaurant name disappears from visible strategy');
  passed('Changing name/type on Home refreshes strategy/calendar and invalidates old saved ideas');

  await evaluate(`document.querySelector('button[aria-label="Sign out"]').click()`);
  await until(() => evaluate(`location.hash === '#/sign-in' && document.querySelector('input[type="password"]') !== null`), 'sign-out returns to sign-in');
  assert.equal(await evaluate(`sessionStorage.getItem('rawaj.preview-session')`), null);
  passed('Sign-out clears the preview session and returns to sign-in');

  assert.deepEqual(runtimeErrors, [], 'No uncaught browser/runtime errors');
  assert.deepEqual(blockedPaidRequests, [], 'No unmocked paid provider routes were triggered');
  const unexpectedErrors = networkErrors.filter(error=>!([404,409].includes(error.status) && /\/strategy$/.test(error.path)) && !(error.status===502 && /\/content-ideas$/.test(error.path)) && error.path!=='/favicon.ico');
  assert.deepEqual(unexpectedErrors, [], 'No unexpected HTTP errors');
  await writeFile(join(outputDir, 'frontend-smoke-results.json'), JSON.stringify({baseUrl,checks,restaurantId,ideaRequests,runtimeErrors,networkErrors}, null, 2));
  console.log(`Browser checks completed: ${checks.length}; screenshots: ${outputDir}`);
} catch (error) {
  if (socket?.readyState === WebSocket.OPEN) {
    await screenshot('frontend-smoke-failure.png').catch(() => {});
    const body = await evaluate('document.body.innerText').catch(()=>'(page unavailable)');
    console.error(body.slice(0,10000));
  }
  console.error(error.stack || error.message);
  process.exitCode = 1;
} finally {
  if (socket?.readyState === WebSocket.OPEN) {
    await command('Browser.close').catch(() => {});
    socket.close();
  }
  if (browser && browser.exitCode === null) browser.kill();
  for (const request of pending.values()) clearTimeout(request.timer);
}
