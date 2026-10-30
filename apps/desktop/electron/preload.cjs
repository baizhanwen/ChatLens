const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('chatlens', Object.freeze({
  api: (method, route, body) => ipcRenderer.invoke('chatlens:api', {method, route, body}),
  openFile: () => ipcRenderer.invoke('chatlens:open-file'),
  demo: () => ipcRenderer.invoke('chatlens:demo'),
  save: (payload) => ipcRenderer.invoke('chatlens:save', payload),
  layout: (payload) => ipcRenderer.invoke('chatlens:layout', payload),
  info: () => ipcRenderer.invoke('chatlens:info'),
  chooseDataDir: () => ipcRenderer.invoke('chatlens:data-dir'),
  notify: (payload) => ipcRenderer.invoke('chatlens:notify', payload)
}));
