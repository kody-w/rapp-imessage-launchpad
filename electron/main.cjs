'use strict';

const {app, BrowserWindow, ipcMain, protocol, session, shell, dialog, Menu, nativeTheme} = require('electron');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const {Backend} = require('./backend.cjs');
const {validate, trustedSender, HELP} = require('./ipc-policy.cjs');

const APP_URL = 'launchpad://app/index.html';
const SMOKE = process.argv.includes('--smoke-test');
const CSP = "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'none'; font-src 'self'; base-uri 'none'; form-action 'none'; frame-src 'none'; object-src 'none'";
protocol.registerSchemesAsPrivileged([{scheme: 'launchpad', privileges: {standard: true, secure: true, supportFetchAPI: true}}]);
app.setName('RAPP iMessage Launchpad');
if (SMOKE) app.setPath('userData', path.resolve('.test-data/electron-profile'));

let window = null;
let backend = null;
let mutating = false;
let interval = null;
let quitting = false;

async function confirm(message, detail) {
  const result = await dialog.showMessageBox(window, {
    type: 'question', buttons: ['Cancel', 'Continue'], defaultId: 0, cancelId: 0,
    noLink: true, message, detail,
  });
  return result.response === 1;
}

function publicRuntime() {
  return {
    appVersion: app.getVersion(), platform: process.platform, packaged: app.isPackaged,
    login: process.platform === 'darwin' ? app.getLoginItemSettings().openAtLogin : false,
    schedulerActive: Boolean(interval), unsigned: true,
  };
}

async function status() {
  try {
    const result = await backend.call(['diagnostics']);
    return {...result, runtime: publicRuntime()};
  } catch (error) {
    return {ok: false, error: 'runtime_unavailable', message: error.message, runtime: publicRuntime()};
  }
}

async function resetSchedule() {
  if (interval) clearInterval(interval);
  interval = null;
  const result = await status();
  if (SMOKE || !result.ok || !result.diagnostics?.send_enabled ||
      !result.diagnostics?.app_schedule || result.schedule?.installed) return;
  interval = setInterval(async () => {
    if (quitting || mutating) return;
    mutating = true;
    try {
      await backend.call(['tick', '--send'], null, 900_000);
    } catch {
      // Status and durable receipts expose failures; never reinterpret them as delivery.
    } finally {
      mutating = false;
    }
  }, 300_000);
}

async function dispatch(action, request) {
  if (action === 'status') return status();
  if (action === 'receipts') return backend.call(['receipts', '--limit', '100', '--refresh']);
  if (action === 'verify') return backend.call(['verify']);
  if (action === 'openHelp') {
    if (request === 'messages') {
      if (process.platform !== 'darwin') throw new Error('Messages is only available on macOS.');
      const child = spawn('/usr/bin/open', ['-a', 'Messages'], {shell: false, stdio: 'ignore'});
      await new Promise((resolve, reject) => {
        child.once('error', reject);
        child.once('exit', code => code === 0 ? resolve() : reject(new Error('Messages could not be opened.')));
      });
    } else if (request === 'data') {
      const error = await shell.openPath(app.getPath('userData'));
      if (error) throw new Error('The private application-data directory could not be opened.');
    } else {
      if (request === 'automation' && process.platform !== 'darwin') throw new Error('Automation settings are macOS-only.');
      await shell.openExternal(HELP[request]);
    }
    return {ok: true};
  }
  if (action === 'setup') {
    if (!await confirm('Configure the local pipeline?', request.mode === 'existing' ?
      'Reuse the detected canonical runtime and recipient. Existing jobs and permissions will not be changed.' :
      'Create a private canonical runtime for this Mac. No message will be sent until you explicitly request a self-test or enable sends.')) return {ok: true, cancelled: true};
    return backend.call(['setup', '--stdin'], request);
  }
  if (action === 'importSources') {
    const choice = await dialog.showOpenDialog(window, {
      title: 'Choose your private source configuration',
      properties: ['openFile'], filters: [{name: 'JSON configuration', extensions: ['json']}],
    });
    if (choice.canceled || choice.filePaths.length !== 1) return {ok: true, cancelled: true};
    if (!await confirm('Replace the explicit source configuration?', 'Scenario modules are trusted local code. Only import source settings you understand. The chosen JSON remains private and is never returned to the renderer or uploaded.')) return {ok: true, cancelled: true};
    return backend.call(['sources', '--set-json', choice.filePaths[0]]);
  }
  if (action === 'run') {
    if (request.send && !await confirm('Evaluate and queue real findings?', 'Only ready, evidence-backed proposals passing the shared policy can enter the existing outbox. Queued is not delivered.')) return {ok: true, cancelled: true};
    return backend.call(['run', request.scenario, '--summary', ...(request.send ? ['--send'] : [])], null, 900_000);
  }
  if (action === 'settings') {
    if ((request.send_enabled === true || request.app_schedule === true) &&
        !await confirm('Allow evidence-backed message production?', 'Enabled scenarios share one recipient, daily budget, dedupe policy, and quiet hours. App scheduling runs every five minutes only while Launchpad is open.')) return {ok: true, cancelled: true};
    const result = await backend.call(['settings', '--stdin'], request);
    if (result.ok) await resetSchedule();
    return result;
  }
  if (action === 'selfTest') {
    if (!await confirm('Request one onboarding message?', 'This sends a clearly labeled self-test to the configured recipient, subject to quiet hours and budget. Existing devices use their already-installed drainer. A new Mac uses the same canonical core and may prompt for Automation. Exit 0 never proves delivery.')) return {ok: true, cancelled: true};
    return backend.call(['self-test', '--send', '--confirm'], null, 180_000);
  }
  if (action === 'confirmReceipt') {
    if (!await confirm('Did you actually receive this message?', 'Continue only after checking the recipient device. This records a distinct user-confirmed receipt, not machine-verified delivery.')) return {ok: true, cancelled: true};
    return backend.call(['confirm', request, '--received', '--yes']);
  }
  if (action === 'login') {
    if (process.platform !== 'darwin' || !app.isPackaged) throw new Error('Login startup is available only in the installed macOS application.');
    if (request && !await confirm('Open Launchpad at login?', 'This starts the app when you sign in. Sending still requires explicit consent and scheduling settings.')) return {ok: true, cancelled: true};
    app.setLoginItemSettings({openAtLogin: request, openAsHidden: false});
    return {ok: true, login: app.getLoginItemSettings().openAtLogin};
  }
  if (action === 'schedule') {
    if (request.action !== 'status' && !await confirm(
      request.action === 'install' ? 'Install a persistent five-minute schedule?' : 'Remove the Launchpad schedule?',
      'Only com.rapp.imessage-launchpad.scheduler is affected. Existing Storykeeper jobs are never changed. Existing mode creates a producer only, not a duplicate drainer. New-Mac mode uses the canonical core.')) return {ok: true, cancelled: true};
    const result = await backend.call(['schedule', request.action, ...(request.action === 'install' ? ['--send'] : [])]);
    if (result.ok) await resetSchedule();
    return result;
  }
  throw new Error('Unsupported action.');
}

function registerIPC() {
  const actions = ['status', 'setup', 'run', 'settings', 'receipts', 'selfTest', 'confirmReceipt', 'verify', 'openHelp', 'login', 'schedule', 'importSources'];
  for (const action of actions) {
    ipcMain.handle('launchpad:' + action, async (event, input) => {
      if (!trustedSender(event, window, APP_URL)) return {ok: false, error: 'untrusted_sender', message: 'This request was rejected.'};
      let acquired = false;
      try {
        const request = validate(action, input);
        const writes = !['status', 'receipts', 'verify', 'openHelp'].includes(action);
        if (writes && mutating) throw new Error('Another local operation is still running.');
        if (writes) { mutating = true; acquired = true; }
        return await dispatch(action, request);
      } catch (error) {
        return {ok: false, error: 'local_operation_failed', message: error.message};
      } finally {
        if (acquired) mutating = false;
      }
    });
  }
}

function createWindow() {
  window = new BrowserWindow({
    width: 1220, height: 860, minWidth: 920, minHeight: 660,
    title: 'RAPP iMessage Launchpad', show: false,
    titleBarStyle: process.platform === 'darwin' ? 'hiddenInset' : 'default',
    backgroundColor: nativeTheme.shouldUseDarkColors ? '#0c1018' : '#f5f6f8',
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true, nodeIntegration: false, sandbox: true,
      webSecurity: true, allowRunningInsecureContent: false,
      spellcheck: false, nodeIntegrationInWorker: false,
    },
  });
  window.webContents.setWindowOpenHandler(() => ({action: 'deny'}));
  window.webContents.on('will-navigate', event => event.preventDefault());
  window.webContents.on('will-attach-webview', event => event.preventDefault());
  window.once('ready-to-show', () => { if (!SMOKE) window.show(); });
  window.on('closed', () => { window = null; });
  window.loadURL(APP_URL);
  if (SMOKE) {
    window.webContents.once('did-finish-load', async () => {
      try {
        const security = await window.webContents.executeJavaScript(`(async () => {
          const status = await window.launchpad.status();
          const refused = await window.launchpad.run({scenario:'../outside',send:false});
          let connectionDenied = false;
          try { await fetch('launchpad://app/package.json'); } catch { connectionDenied = true; }
          return {
            title: document.title,
            bridge: typeof window.launchpad?.status,
            nodeAbsent: typeof window.require === 'undefined' && typeof window.process === 'undefined',
            tabs: document.querySelectorAll('[data-tab]').length,
            csp: document.querySelector('meta[http-equiv="Content-Security-Policy"]').content,
            realIPC: status.ok && !status.diagnostics.configured,
            rejectedUnsafeInput: refused.ok === false,
            connectionDenied
          };
        })()`);
        const arbitraryAsset = await session.defaultSession.fetch('launchpad://app/package.json');
        const runtime = await backend.findPython();
        const diagnostic = await backend.call(['diagnostics']);
        const report = {
          ok: security.nodeAbsent && security.bridge === 'function' && security.tabs === 4 &&
            security.realIPC && security.rejectedUnsafeInput && security.connectionDenied &&
            arbitraryAsset.status === 404 &&
            Boolean(diagnostic.ok && !diagnostic.diagnostics.configured),
          security, python: runtime.version,
          sandbox: window.webContents.getLastWebPreferences().sandbox,
          arbitraryAssetDenied: arbitraryAsset.status === 404,
          packaged: app.isPackaged,
        };
        const target = path.resolve('.test-data/electron-smoke.json');
        fs.mkdirSync(path.dirname(target), {recursive: true});
        fs.writeFileSync(target, JSON.stringify(report, null, 2) + '\n', {mode: 0o600});
        const image = await window.webContents.capturePage();
        fs.writeFileSync(path.resolve('.test-data/electron-smoke.png'), image.toPNG(), {mode: 0o600});
        app.exit(report.ok ? 0 : 1);
      } catch (error) {
        console.error('Smoke test failed:', error.message);
        app.exit(1);
      }
    });
  }
}

app.whenReady().then(async () => {
  const assets = {
    '/index.html': ['renderer/index.html', 'text/html; charset=utf-8'],
    '/styles.css': ['renderer/styles.css', 'text/css; charset=utf-8'],
    '/renderer.js': ['renderer/renderer.js', 'text/javascript; charset=utf-8'],
    '/icon.svg': ['../assets/icon.svg', 'image/svg+xml'],
  };
  protocol.handle('launchpad', request => {
    const url = new URL(request.url);
    const asset = assets[url.pathname];
    if (request.method !== 'GET' || url.hostname !== 'app' || !asset || url.search || url.hash) {
      return new Response('Not found', {status: 404});
    }
    return new Response(fs.readFileSync(path.join(__dirname, asset[0])), {
      headers: {'Content-Type': asset[1], 'Content-Security-Policy': CSP, 'X-Content-Type-Options': 'nosniff'},
    });
  });
  session.defaultSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  session.defaultSession.setPermissionCheckHandler(() => false);
  session.defaultSession.webRequest.onBeforeRequest((details, callback) => {
    callback({cancel: !details.url.startsWith('launchpad://app/')});
  });
  backend = new Backend({
    root: app.isPackaged ? path.join(process.resourcesPath, 'python') : path.resolve(__dirname, '..'),
    config: path.join(app.getPath('userData'), 'config.json'),
  });
  registerIPC();
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    {label: app.name, submenu: [{role: 'about'}, {type: 'separator'}, {role: 'hide'}, {role: 'hideOthers'}, {type: 'separator'}, {role: 'quit'}]},
    {label: 'Edit', submenu: [{role: 'undo'}, {role: 'redo'}, {type: 'separator'}, {role: 'cut'}, {role: 'copy'}, {role: 'paste'}, {role: 'selectAll'}]},
    {label: 'Window', submenu: [{role: 'minimize'}, {role: 'zoom'}, {role: 'close'}]},
  ]));
  createWindow();
  if (!SMOKE) await resetSchedule();
});

app.on('activate', () => { if (!window && backend) createWindow(); });
app.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit(); });
app.on('before-quit', () => {
  quitting = true;
  if (interval) clearInterval(interval);
  backend?.stop();
});
