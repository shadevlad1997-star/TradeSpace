// Isolated widget behavior; real layout is checked separately in the browser.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.resolve(__dirname, '../../app/static/tradespace/js/merchant-rate.js'), 'utf8');

const goodQuote = (extra = {}) => ({
  source: 'rapira_live', stale: false, rate_rub: '101.2345', fee_usdt: '5.00',
  available_rub: '10000.00', max_request_usdt: '93.78', valid_for_seconds: 59,
  fetched_at: '2026-09-28T12:00:00Z', ...extra,
});

function setup(body = goodQuote()) {
  let now = Date.parse('2026-09-28T12:00:00Z'), nextId = 1;
  const timers = new Map(), handlers = new Map();
  const state = {body, status: 200, calls: 0, pending: false};
  const element = () => ({
    textContent: '', hidden: false, disabled: false, attributes: {}, dataset: {}, listeners: {},
    classList: {add() {}, remove() {}},
    setAttribute(k, v) {this.attributes[k] = String(v);},
    removeAttribute(k) {delete this.attributes[k];},
    addEventListener(k, fn) {this.listeners[k] = fn;},
  });
  const form = element(), fields = element(), amount = element(), address = element();
  amount.value = '12.50'; address.value = 'T-user-entered-address';
  const dialog = {open: false, close() {this.open = false;}};
  form.dataset.submitUrl = '/merchant/cabinet/settlements/request';
  form.querySelector = key => ({'[name="amount_usdt"]': amount, dialog})[key];
  const elements = new Map([
    ['[data-rate-form]', form], ['[data-rate-fields]', fields],
    ...['widget', 'widget-value', 'form-value', 'maximum', 'time', 'warning', 'loading', 'panel', 'fee']
      .map(key => [`[data-rate-${key}]`, element()]),
    ['[data-rate-balance] .ts-money__value', element()],
  ]);
  const root = {dataset: {rateAvailable: 'false', rateValidFor: '0', rateUrl: '/merchant/cabinet/rates/usdt-rub'},
    querySelector: key => elements.get(key)};
  const document = {hidden: false, querySelector: () => root,
    addEventListener: (key, fn) => handlers.set(key, fn)};
  const window = {
    setTimeout(fn, delay) {const id = nextId++; timers.set(id, {fn, at: now + delay}); return id;},
    clearTimeout(id) {timers.delete(id);},
    addEventListener(key, fn) {handlers.set(key, fn);},
  };
  class ClockDate extends Date {static now() {return now;}}
  const fetch = async (_url, options) => {
    state.calls++;
    state.options = options;
    if (state.pending) return new Promise((resolve, reject) => {
      options.signal.addEventListener('abort', () => reject(new Error('test timeout')), {once: true});
    });
    return {ok: state.status === 200, status: state.status, json: async () => state.body};
  };
  vm.runInNewContext(source, {document, window, fetch, AbortController, Date: ClockDate, Intl});
  const settle = () => new Promise(resolve => setImmediate(resolve));
  return {state, form, fields, amount, address, dialog, document, handlers, elements, settle,
    async advance(ms) {
      now += ms;
      for (const [id, timer] of [...timers]) {
        if (timer.at <= now && timers.delete(id)) timer.fn();
      }
      await settle();
    }};
}

test('quote and form agree; refresh preserves amount, destination, and decimal formatting', async () => {
  const ui = setup(); await ui.settle();
  assert.equal(ui.elements.get('[data-rate-widget-value]').textContent, '1 USDT = 101.2345 RUB');
  assert.equal(ui.elements.get('[data-rate-form-value]').textContent, '101.2345 RUB');
  assert.equal(ui.amount.attributes.max, '93.78');
  assert.equal(ui.fields.disabled, false);
  assert.equal(ui.form.attributes.action, ui.form.dataset.submitUrl);
  ui.state.body = goodQuote({rate_rub: '102.0000'});
  await ui.advance(20000);
  assert.equal(ui.elements.get('[data-rate-form-value]').textContent, '102.0000 RUB');
  assert.equal(ui.amount.value, '12.50');
  assert.equal(ui.address.value, 'T-user-entered-address');
  assert.equal(ui.state.options.method, 'GET');
  assert.equal(ui.state.options.cache, 'no-store');
});

test('HTTP error closes confirmation and blocks sending; recovery keeps entered values', async () => {
  const ui = setup(); await ui.settle();
  ui.dialog.open = true; ui.state.status = 503;
  await ui.advance(20000);
  assert.equal(ui.fields.disabled, true);
  assert.equal(ui.dialog.open, false);
  assert.equal(ui.form.attributes.action, undefined);
  assert.equal(ui.elements.get('[data-rate-widget-value]').textContent, 'Курс временно недоступен');
  ui.state.status = 200;
  await ui.advance(20000);
  assert.equal(ui.fields.disabled, false);
  assert.equal(ui.amount.value, '12.50');
  assert.equal(ui.address.value, 'T-user-entered-address');
});

test('freshness expiry blocks form before the next poll and prevents stale submit', async () => {
  const ui = setup(goodQuote({valid_for_seconds: 1})); await ui.settle();
  await ui.advance(1001);
  let prevented = false;
  ui.form.listeners.submit({preventDefault() {prevented = true;}});
  assert.equal(prevented, true);
  assert.equal(ui.fields.disabled, true);
  assert.equal(ui.state.calls, 1);
});

test('fallback, stale, invalid, missing and zero quotes never appear as a working rate', async () => {
  for (const extra of [{source:'SETTLEMENT_USDT_RUB_RATE'}, {stale:true}, {rate_rub:'0'},
    {rate_rub:'NaN'}, {rate_rub:'Infinity'}, {rate_rub:'-1'}, {valid_for_seconds:0},
    {valid_for_seconds:undefined}, {fetched_at:'invalid'}]) {
    const ui = setup(goodQuote(extra)); await ui.settle();
    assert.equal(ui.fields.disabled, true, JSON.stringify(extra));
    assert.equal(ui.elements.get('[data-rate-widget-value]').textContent, 'Курс временно недоступен');
  }
});

test('timeout aborts polling and does not discard entered values', async () => {
  const ui = setup(); await ui.settle(); ui.state.pending = true;
  await ui.advance(20000); await ui.advance(8000);
  assert.equal(ui.state.options.signal.aborted, true);
  assert.equal(ui.fields.disabled, true);
  assert.equal(ui.amount.value, '12.50');
});

test('hidden pages pause polling; focus changes do not multiply requests', async () => {
  const ui = setup(); await ui.settle();
  ui.document.hidden = true; ui.handlers.get('visibilitychange')();
  await ui.advance(20000); assert.equal(ui.state.calls, 1);
  ui.document.hidden = false;
  for (let i = 0; i < 5; i++) ui.handlers.get('visibilitychange')();
  await ui.settle(); assert.equal(ui.state.calls, 2);
});

test('successful submit is not re-enabled or repriced while navigation is pending', async () => {
  const ui = setup(); await ui.settle();
  ui.form.listeners.submit({preventDefault() {assert.fail('fresh submit blocked');}});
  await ui.advance(60000);
  assert.equal(ui.state.calls, 1);
});

test('expired session stops refresh without displaying technical error payload', async () => {
  const ui = setup(); await ui.settle();
  ui.state.status = 401; ui.state.body = {detail: 'private technical error'};
  await ui.advance(20000); await ui.advance(60000);
  assert.equal(ui.state.calls, 2);
  assert.equal(ui.fields.disabled, true);
  assert.equal(ui.elements.get('[data-rate-widget-value]').textContent, 'Курс временно недоступен');
});
