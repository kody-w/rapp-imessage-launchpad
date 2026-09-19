import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {spawnSync} from 'node:child_process';

function walk(root) {
  return fs.readdirSync(root, {withFileTypes: true}).flatMap(entry => {
    const file = path.join(root, entry.name);
    return entry.isDirectory() ? walk(file) : [file];
  });
}
for (const file of ['electron', 'scripts', 'tests/js'].flatMap(walk).filter(file => /\.(cjs|mjs|js)$/.test(file))) {
  const check = spawnSync(process.execPath, ['--check', file], {encoding: 'utf8'});
  assert.equal(check.status, 0, check.stderr);
}
const manifest = JSON.parse(fs.readFileSync('package.json', 'utf8'));
const lock = JSON.parse(fs.readFileSync('package-lock.json', 'utf8'));
assert.deepEqual(manifest.devDependencies, lock.packages[''].devDependencies);
for (const version of Object.values(manifest.devDependencies)) assert.match(version, /^\d+\.\d+\.\d+$/);
for (const file of walk('protocol').filter(file => file.endsWith('.json'))) JSON.parse(fs.readFileSync(file, 'utf8'));
const main = fs.readFileSync('electron/main.cjs', 'utf8');
for (const rule of [/contextIsolation:\s*true/, /nodeIntegration:\s*false/, /sandbox:\s*true/, /setWindowOpenHandler/, /setPermissionRequestHandler/, /trustedSender/]) assert.match(main, rule);
const renderer = fs.readFileSync('electron/renderer/renderer.js', 'utf8');
assert.doesNotMatch(renderer, /innerHTML|insertAdjacentHTML|eval\(|require\(/);
const html = fs.readFileSync('electron/renderer/index.html', 'utf8');
assert.match(html, /connect-src 'none'/);
assert.doesNotMatch(html, /unsafe-inline|unsafe-eval|https?:\/\//);
const files = spawnSync('git', ['ls-files', '--cached', '--others', '--exclude-standard'], {encoding: 'utf8'});
assert.equal(files.status, 0);
for (const file of new Set(files.stdout.trim().split('\n').filter(Boolean))) {
  if (!/\.(py|js|cjs|mjs|json|md|html|css|toml|yml|yaml|svg|plist)$/.test(file)) continue;
  const content = fs.readFileSync(file, 'utf8');
  assert.doesNotMatch(content, /\/Users\/[A-Za-z0-9._-]+\//, `${file}: machine-specific user path`);
  assert.doesNotMatch(content, /\bgh[pousr]_[A-Za-z0-9]{30,}\b/, `${file}: credential-like material`);
  assert.doesNotMatch(content, /-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----/, `${file}: private key`);
}
console.log('Syntax, dependency pinning, renderer isolation, protocol JSON, and public-source checks passed.');
