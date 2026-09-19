'use strict';

const {spawn} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

class Backend {
  constructor({root, config, platform = process.platform, spawnProcess = spawn}) {
    this.root = root;
    this.config = config;
    this.platform = platform;
    this.spawnProcess = spawnProcess;
    this.python = null;
    this.children = new Set();
  }

  async capture(executable, args, input = '', timeout = 10_000) {
    return new Promise((resolve, reject) => {
      let child;
      try {
        child = this.spawnProcess(executable, args, {
          shell: false, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'],
          env: {...process.env, PYTHONPATH: '', PYTHONDONTWRITEBYTECODE: '1'},
        });
      } catch {
        reject(new Error('Could not start the local Python runtime.'));
        return;
      }
      this.children.add(child);
      const output = [];
      let bytes = 0;
      let stopped = false;
      let reason = null;
      const stop = message => {
        if (stopped) return;
        stopped = true;
        reason = message;
        child.kill('SIGTERM');
        const hard = setTimeout(() => child.kill('SIGKILL'), 2_000);
        hard.unref();
      };
      const timer = setTimeout(() => stop('Local operation timed out; no successful send or delivery is assumed.'), timeout);
      child.stdout.on('data', chunk => {
        bytes += chunk.length;
        if (bytes > 1_048_576) stop('Local operation exceeded its output limit.');
        else output.push(chunk);
      });
      child.stderr.on('data', chunk => {
        bytes += chunk.length;
        if (bytes > 1_048_576) stop('Local operation exceeded its output limit.');
      });
      child.on('error', () => {
        clearTimeout(timer);
        this.children.delete(child);
        reject(new Error('Could not start the local Python runtime.'));
      });
      child.on('close', code => {
        clearTimeout(timer);
        this.children.delete(child);
        if (reason) return reject(new Error(reason));
        try {
          const result = JSON.parse(Buffer.concat(output).toString('utf8'));
          if (!result || typeof result !== 'object' || Array.isArray(result)) throw new Error();
          if (code && result.ok !== false) throw new Error();
          resolve(result);
        } catch {
          reject(new Error('The local SDK returned an invalid response; private process output was withheld.'));
        }
      });
      child.stdin.on('error', () => {});
      child.stdin.end(input);
    });
  }

  async findPython() {
    if (this.python) return this.python;
    const custom = process.env.RAPP_LAUNCHPAD_PYTHON;
    const candidates = this.platform === 'win32' ? ['python3.exe', 'python.exe'] :
      ['/opt/homebrew/bin/python3', '/usr/local/bin/python3', '/usr/bin/python3'];
    if (custom && path.isAbsolute(custom)) candidates.unshift(custom);
    for (const executable of [...new Set(candidates)]) {
      if (path.isAbsolute(executable) && !fs.existsSync(executable)) continue;
      if (this.platform === 'darwin' && executable === '/usr/bin/python3' &&
          !fs.existsSync('/Library/Developer/CommandLineTools/usr/bin/python3') &&
          !fs.existsSync('/Applications/Xcode.app/Contents/Developer/usr/bin/python3')) continue;
      try {
        const result = await this.capture(executable, [
          '-I', '-B', '-c',
          'import json,sys; print(json.dumps({"major":sys.version_info.major,"minor":sys.version_info.minor,"version":sys.version.split()[0]}))',
        ]);
        if (result.major === 3 && result.minor >= 9) {
          this.python = {executable, version: result.version};
          return this.python;
        }
      } catch {
        // Candidate failure is not permission or delivery evidence.
      }
    }
    throw new Error('Python 3.9+ was not found. Install Python from python.org, then restart Launchpad. No Python runtime is bundled.');
  }

  async call(args, data = null, timeout = 90_000) {
    const runtime = await this.findPython();
    const entry = path.join(this.root, 'rapp_launchpad', '_cli_entry.py');
    return this.capture(runtime.executable, ['-I', '-B', entry, '--config', this.config, ...args],
      data === null ? '' : JSON.stringify(data), timeout);
  }

  stop() {
    for (const child of this.children) child.kill('SIGTERM');
  }
}

module.exports = {Backend};
