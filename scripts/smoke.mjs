import fs from 'node:fs';
import path from 'node:path';
import {spawn} from 'node:child_process';
import {createRequire} from 'node:module';
const require = createRequire(import.meta.url);
const packaged = process.argv.includes('--packaged');
const bundleDirectory = fs.existsSync('release/mac-universal/RAPP iMessage Launchpad.app') ?
  'mac-universal' : 'mac-arm64';
const executable = packaged ?
  path.resolve(`release/${bundleDirectory}/RAPP iMessage Launchpad.app/Contents/MacOS/RAPP iMessage Launchpad`) :
  require('electron');
fs.mkdirSync('.test-data', {recursive: true});
fs.mkdirSync('.build-cache', {recursive: true});
const report = path.resolve('.test-data/electron-smoke.json');
if (fs.existsSync(report)) fs.unlinkSync(report);
const child = spawn(executable, [...(packaged ? [] : ['.']), '--smoke-test'], {
  stdio: ['ignore', 'pipe', 'pipe'], shell: false,
  env: {...process.env, TMPDIR: path.resolve('.build-cache')},
});
let output = '';
for (const stream of [child.stdout, child.stderr]) stream.on('data', chunk => { if (output.length < 20_000) output += chunk; });
const timer = setTimeout(() => child.kill('SIGTERM'), 60_000);
const code = await new Promise((resolve, reject) => {
  child.once('error', reject);
  child.once('exit', resolve);
});
clearTimeout(timer);
if (code !== 0 || !fs.existsSync(report)) {
  console.error(output);
  throw new Error(`Electron ${packaged ? 'packaged' : 'development'} smoke test failed (${code}).`);
}
const result = JSON.parse(fs.readFileSync(report, 'utf8'));
if (!result.ok || !result.sandbox) throw new Error('Electron smoke security assertions failed.');
console.log(JSON.stringify(result, null, 2));
