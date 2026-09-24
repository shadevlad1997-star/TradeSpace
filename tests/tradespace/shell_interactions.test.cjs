// Isolated event-handler tests, not browser layout / touch acceptance.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.resolve(__dirname, '../../app/static/tradespace/js/shell.js'), 'utf8');

function setup() {
  const callbacks = new Map();
  const document = {body: {style: {overflow: ''}}, activeElement: null};
  function element() {
    const listeners = new Map();
    return {
      hidden: true, disabled: false, attributes: {},
      addEventListener(type, listener) {listeners.set(type, listener);},
      fire(type, event = {}) {listeners.get(type)?.(event);},
      setAttribute(key, value) {this.attributes[key] = value;},
      focus() {document.activeElement = this;},
    };
  }
  const toggle = element(), first = element(), last = element(), sheet = element();
  const summary = element(), inside = element();
  const menu = {open: false, contains: node => node === summary || node === inside,
    querySelector: () => summary};
  sheet.querySelector = () => first;
  sheet.querySelectorAll = selector => selector === '[data-mobile-more-close]' ? [] : [first, last];
  Object.assign(document, {
    getElementById: id => id === 'mobile-more-sheet' ? sheet : null,
    querySelector: selector => selector === '[data-mobile-more]' ? toggle : null,
    querySelectorAll: selector => selector === '.ts-account, .ts-more-menu' ? [menu] : [],
    addEventListener(type, listener) {
      callbacks.set(type, [...(callbacks.get(type) || []), listener]);
    },
  });
  const media = [];
  const window = {addEventListener() {}, matchMedia(query) {
    return {matches: query.includes('max-width'), addEventListener(_, listener) {media.push(listener);}};
  }};
  vm.runInNewContext(source, {document, window});
  return {document, toggle, first, last, sheet, summary, inside, menu, media,
    dispatch(type, event) {for (const fn of callbacks.get(type) || []) fn(event);}};
}

test('desktop Escape closes the menu and returns focus to its summary', () => {
  const ui = setup();
  ui.menu.open = true;
  ui.inside.focus();
  ui.dispatch('keydown', {key: 'Escape'});
  assert.equal(ui.menu.open, false);
  assert.equal(ui.document.activeElement, ui.summary);
});

test('click inside preserves desktop menu; click outside closes it without stealing focus', () => {
  const ui = setup();
  ui.menu.open = true;
  ui.dispatch('click', {target: ui.inside});
  assert.equal(ui.menu.open, true);
  ui.first.focus();
  ui.dispatch('click', {target: ui.first});
  assert.equal(ui.menu.open, false);
  assert.equal(ui.document.activeElement, ui.first);
});

test('mobile More opens, traps Tab in both directions and Escape restores focus', () => {
  const ui = setup();
  ui.toggle.fire('click');
  assert.equal(ui.sheet.hidden, false);
  assert.equal(ui.toggle.attributes['aria-expanded'], 'true');
  assert.equal(ui.document.activeElement, ui.first);
  assert.equal(ui.document.body.style.overflow, 'hidden');
  let prevented = 0;
  ui.sheet.fire('keydown', {key: 'Tab', shiftKey: true, preventDefault() {prevented++;}});
  assert.equal(ui.document.activeElement, ui.last);
  ui.sheet.fire('keydown', {key: 'Tab', shiftKey: false, preventDefault() {prevented++;}});
  assert.equal(ui.document.activeElement, ui.first);
  assert.equal(prevented, 2);
  ui.dispatch('keydown', {key: 'Escape'});
  assert.equal(ui.sheet.hidden, true);
  assert.equal(ui.toggle.attributes['aria-expanded'], 'false');
  assert.equal(ui.document.body.style.overflow, '');
  assert.equal(ui.document.activeElement, ui.toggle);
});

test('switching to desktop releases mobile scroll lock', () => {
  const ui = setup();
  ui.toggle.fire('click');
  ui.media[0]({matches: true});
  assert.equal(ui.sheet.hidden, true);
  assert.equal(ui.document.body.style.overflow, '');
});
