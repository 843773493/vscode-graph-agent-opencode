export const HostToWebviewMessageType = {
  init: 'init',
  state: 'state',
  error: 'error',
  sessionCreated: 'sessionCreated',
  messageAccepted: 'messageAccepted',
  jobEvent: 'jobEvent',
};

export const WebviewToHostMessageType = {
  ready: 'ready',
  refresh: 'refresh',
  createSession: 'createSession',
  selectSession: 'selectSession',
  sendMessage: 'sendMessage',
  debug: 'debug',
  updateSession: 'updateSession',
};
