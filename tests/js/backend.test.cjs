'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const {Backend} = require('../../electron/backend.cjs');

const root = path.resolve(__dirname, '../..');
const backend = new Backend({root, config: path.join(root, '.test-data/node-unconfigured.json')});

test('discovers a real Python 3.9+ and runs read-only unconfigured diagnostics', async () => {
  const python = await backend.findPython();
  assert.match(python.version, /^3\./);
  const result = await backend.call(['diagnostics']);
  assert.equal(result.ok, true);
  assert.equal(result.diagnostics.configured, false);
  assert.equal(result.diagnostics.automation, 'unknown-until-explicit-self-test');
  assert.equal(result.diagnostics.ready_to_queue, false);
});

test('invalid child output is withheld rather than leaked to renderer', async () => {
  const python = await backend.findPython();
  await assert.rejects(
    backend.capture(python.executable, ['-I', '-c', 'print("SYNTHETIC_PRIVATE_OUTPUT")']),
    error => !error.message.includes('SYNTHETIC_PRIVATE_OUTPUT') && /invalid response/.test(error.message),
  );
});

test('worker wall time is bounded', async () => {
  const python = await backend.findPython();
  const start = Date.now();
  await assert.rejects(backend.capture(python.executable, ['-I', '-c', 'import time; time.sleep(30)'], '', 150), /timed out/);
  assert.ok(Date.now() - start < 5_000);
});

test('worker stdout and stderr have a shared bound', async () => {
  const python = await backend.findPython();
  await assert.rejects(backend.capture(python.executable, ['-I', '-c', 'print("x"*2000000)']), /output limit/);
});

test('UTF-8 split across child output chunks is preserved', async () => {
  const python = await backend.findPython();
  const result = await backend.capture(python.executable, ['-I', '-c',
    'import sys,time; b=\'{"text":"🌿"}\'.encode(); sys.stdout.buffer.write(b[:10]); sys.stdout.buffer.flush(); time.sleep(.05); sys.stdout.buffer.write(b[10:])']);
  assert.equal(result.text, '🌿');
});
