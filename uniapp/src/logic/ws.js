// uniapp/src/logic/ws.js —— 通知 WS 客户端：首帧鉴权 / 心跳 pong / 指数退避重连 / latest_inbox_id 对齐

function defaultFactory(url) {
  const u = globalThis.uni;
  return u?.connectSocket ? u.connectSocket({ url }) : new WebSocket(url);
}

export function createNotifySocket({
  url, token, socketFactory = defaultFactory, onMessage = () => {},
  onReconnect = () => {}, scheduler = (fn, ms) => setTimeout(fn, ms),
  backoff = { base: 1000, max: 30000 },
} = {}) {
  let attempts = 0;
  let closedByUser = false;
  const state = { socket: null, connected: false };

  function send(obj) {
    state.socket?.send?.(JSON.stringify(obj));
  }

  function handleFrame(ev) {
    let msg;
    try { msg = JSON.parse(ev?.data); } catch { return; }
    if (msg.type === "hello") {
      onReconnect({ sinceId: msg.latest_inbox_id ?? null, unreadCount: msg.unread_count ?? 0 });
      return;
    }
    if (msg.type === "msg") { onMessage(msg); send({ type: "pong" }); return; }
    if (msg.type === "ping") send({ type: "pong" });
  }

  function bind(sock) {
    const on = (web, uniEvt, handler) => {
      if (typeof sock[web] !== "undefined") sock[web] = handler;
      else if (sock[uniEvt]) sock[uniEvt](handler);
    };
    on("onopen", "onOpen", () => {
      attempts = 0;
      state.connected = true;
      send({ type: "auth", token });
    });
    on("onmessage", "onMessage", handleFrame);
    on("onclose", "onClose", () => { state.connected = false; scheduleReconnect(); });
  }

  function scheduleReconnect() {
    if (closedByUser) return;
    const exp = Math.min(backoff.base * 2 ** attempts, backoff.max);
    const jitter = exp * (0.5 + Math.random() * 0.5);
    attempts += 1;
    scheduler(() => { if (!closedByUser) connect(); }, Math.round(jitter));
  }

  function connect() {
    const sock = socketFactory(url);
    state.socket = sock;
    bind(sock);
    return sock;
  }

  return {
    connect,
    close() { closedByUser = true; state.connected = false; state.socket?.close?.(); },
    get socket() { return state.socket; },
    get _socket() { return state.socket; },
    get connected() { return state.connected; }
  };
}
