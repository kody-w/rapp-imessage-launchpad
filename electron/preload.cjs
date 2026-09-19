'use strict';

const {contextBridge, ipcRenderer} = require('electron');

contextBridge.exposeInMainWorld('launchpad', Object.freeze({
  status: () => ipcRenderer.invoke('launchpad:status'),
  setup: value => ipcRenderer.invoke('launchpad:setup', value),
  run: value => ipcRenderer.invoke('launchpad:run', value),
  settings: value => ipcRenderer.invoke('launchpad:settings', value),
  receipts: () => ipcRenderer.invoke('launchpad:receipts'),
  selfTest: () => ipcRenderer.invoke('launchpad:selfTest'),
  confirmReceipt: id => ipcRenderer.invoke('launchpad:confirmReceipt', id),
  verify: () => ipcRenderer.invoke('launchpad:verify'),
  openHelp: destination => ipcRenderer.invoke('launchpad:openHelp', destination),
  login: enabled => ipcRenderer.invoke('launchpad:login', enabled),
  schedule: value => ipcRenderer.invoke('launchpad:schedule', value),
  importSources: () => ipcRenderer.invoke('launchpad:importSources'),
}));
