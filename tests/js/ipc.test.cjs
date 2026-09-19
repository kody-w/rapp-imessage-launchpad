'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {validate, trustedSender, HELP} = require('../../electron/ipc-policy.cjs');

test('IPC rejects arbitrary commands, paths, methods, and extra fields', () => {
  for (const [action, value] of [
    ['exec', {command: 'arbitrary'}],
    ['openHelp', 'file:///private/file'],
    ['openHelp', 'https://unapproved.invalid'],
    ['run', {scenario: '../outbox', send: true}],
    ['run', {scenario: 'future', send: 'yes'}],
    ['run', {scenario: 'future', send: false, command: 'arbitrary'}],
    ['setup', {mode: 'existing', source: '/arbitrary'}],
    ['setup', {mode: 'existing', recipient: 'reader@example.invalid'}],
    ['setup', {mode: 'portable', recipient: 'not a recipient'}],
    ['settings', {home: '/arbitrary'}],
    ['settings', {enabled_scenarios: ['../outbox']}],
    ['settings', {send_enabled: 1}],
    ['confirmReceipt', '../../secrets'],
    ['schedule', {action: 'bootstrap', path: '/arbitrary'}],
    ['status', {paths: ['/private']}],
    ['importSources', {file: '/arbitrary'}],
    ['login', 'true'],
  ]) assert.throws(() => validate(action, value), action);
});

test('IPC accepts only supported narrow inputs', () => {
  assert.deepEqual(validate('run', {scenario: 'future', send: false}), {scenario: 'future', send: false});
  assert.deepEqual(validate('setup', {mode: 'portable', recipient: 'reader@example.invalid'}), {mode: 'portable', recipient: 'reader@example.invalid'});
  assert.deepEqual(validate('settings', {enabled_scenarios: ['future', 'future']}), {enabled_scenarios: ['future']});
  assert.equal(validate('status'), null);
  assert.equal(validate('confirmReceipt', 'a'.repeat(32)), 'a'.repeat(32));
  assert.equal(HELP.python, 'https://www.python.org/downloads/macos/');
});

test('prototype-bearing and array-shaped objects are refused', () => {
  const poisoned = Object.create({send_enabled: true});
  assert.throws(() => validate('settings', poisoned));
  assert.throws(() => validate('settings', []));
  assert.throws(() => validate('settings', JSON.parse('{"__proto__":{"send_enabled":true}}')));
});

test('only the exact top-level application frame is trusted', () => {
  const mainFrame = {url: 'launchpad://app/index.html'};
  const webContents = {mainFrame};
  const window = {isDestroyed: () => false, webContents};
  const event = {sender: webContents, senderFrame: mainFrame};
  assert.equal(trustedSender(event, window, mainFrame.url), true);
  assert.equal(trustedSender({...event, sender: {}}, window, mainFrame.url), false);
  assert.equal(trustedSender({...event, senderFrame: {url: mainFrame.url}}, window, mainFrame.url), false);
  assert.equal(trustedSender(event, window, 'https://remote.invalid'), false);
  assert.equal(trustedSender(event, null, mainFrame.url), false);
});
