'use strict';

const SLUG = /^[a-z][a-z0-9_-]{0,63}$/;
const HELP = Object.freeze({
  automation: 'x-apple.systempreferences:com.apple.preference.security?Privacy_Automation',
  python: 'https://www.python.org/downloads/macos/',
});

function record(value, allowed) {
  if (!value || typeof value !== 'object' || Array.isArray(value) ||
      ![Object.prototype, null].includes(Object.getPrototypeOf(value)) ||
      Object.keys(value).some(key => !allowed.includes(key))) {
    throw new Error('Unsupported request shape.');
  }
  return value;
}

function none(value) {
  if (value !== undefined && value !== null) throw new Error('This action takes no parameters.');
  return null;
}

function boolean(value) {
  if (typeof value !== 'boolean') throw new Error('Expected a boolean.');
  return value;
}

function validate(action, value) {
  if (['status', 'receipts', 'selfTest', 'verify', 'importSources'].includes(action)) return none(value);
  if (action === 'setup') {
    record(value, ['mode', 'recipient']);
    if (!['existing', 'portable'].includes(value.mode)) throw new Error('Choose an available transport mode.');
    if (value.mode === 'existing' && value.recipient !== undefined) throw new Error('Existing recipient is read-only.');
    if (value.mode === 'portable' && (typeof value.recipient !== 'string' || value.recipient.length > 254 ||
        !/^(?:\+[1-9][0-9]{6,14}|[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63})$/.test(value.recipient))) {
      throw new Error('Enter an international +number or an iMessage email address.');
    }
    return {...value};
  }
  if (action === 'run') {
    record(value, ['scenario', 'send']);
    if (typeof value.scenario !== 'string' || !SLUG.test(value.scenario)) throw new Error('Invalid scenario name.');
    return {scenario: value.scenario, send: boolean(value.send)};
  }
  if (action === 'settings') {
    record(value, ['send_enabled', 'app_schedule', 'enabled_scenarios']);
    const result = {};
    for (const key of ['send_enabled', 'app_schedule']) {
      if (value[key] !== undefined) result[key] = boolean(value[key]);
    }
    if (value.enabled_scenarios !== undefined) {
      if (!Array.isArray(value.enabled_scenarios) || value.enabled_scenarios.length > 50 ||
          value.enabled_scenarios.some(name => typeof name !== 'string' || !SLUG.test(name))) {
        throw new Error('Invalid scenario allowlist.');
      }
      result.enabled_scenarios = [...new Set(value.enabled_scenarios)];
    }
    return result;
  }
  if (action === 'confirmReceipt') {
    if (typeof value !== 'string' || !/^[a-f0-9]{32}$/.test(value)) throw new Error('Invalid receipt identifier.');
    return value;
  }
  if (action === 'openHelp') {
    if (!['automation', 'python', 'messages', 'data'].includes(value)) throw new Error('Unsupported help destination.');
    return value;
  }
  if (action === 'login') return boolean(value);
  if (action === 'schedule') {
    record(value, ['action']);
    if (!['install', 'uninstall', 'status'].includes(value.action)) throw new Error('Unsupported schedule operation.');
    return {action: value.action};
  }
  throw new Error('Unsupported action.');
}

function trustedSender(event, window, appURL) {
  return Boolean(window && !window.isDestroyed() &&
    event.sender === window.webContents &&
    event.senderFrame === window.webContents.mainFrame &&
    event.senderFrame.url === appURL);
}

module.exports = {validate, trustedSender, HELP};
