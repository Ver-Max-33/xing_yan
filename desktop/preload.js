// The only bridge between the sandboxed renderer and the main process (spec §3.3).
const { contextBridge, ipcRenderer } = require("electron")

contextBridge.exposeInMainWorld("wb", {
  platform: process.platform,
  openExternal: (url) => ipcRenderer.invoke("wb:open-external", String(url)),
  setKeepAwake: (on) => ipcRenderer.invoke("wb:keep-awake", Boolean(on)),
  // 下载并安装: the main process waits for the backend's checked download, then closes the app and installs it
  installUpdate: () => ipcRenderer.invoke("wb:install-update"),
  // Windows: the window's buttons (drawn over the top row) in the page's colours and the row's height
  setTitleBar: (color, symbolColor, height) => ipcRenderer.invoke("wb:title-bar", String(color), String(symbolColor), Number(height)),
})
