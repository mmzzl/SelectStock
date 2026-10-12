// uniapp/src/logic/ws.js —— 通知 WS 客户端：首帧鉴权 / 定时心跳 / 指数退避重连 / latest_inbox_id 对齐

// 应用层心跳不是为了防空闲断连（服务端 uvicorn 每 20s 发协议层 ping，隧道不会被掐），
// 而是让客户端能发现半开连接：隧道悄悄断了不会触发 onclose，页面就再也不更新。
// 容忍 2 拍没回应再判死，免得移动网络偶尔丢一个 pong 就重连一次（重连要重新鉴权并按 since_id 补拉）。
const DEFAULT_HEARTBEAT = { interval: 20000, tolerateMissed: 2 };

function defaultFactory(url) {
  const u = globalThis.uni;
  return u?.connectSocket ? u.connectSocket({ url }) : new WebSocket(url);
}

export function createNotifySocket({
  url, token, socketFactory = defaultFactory, onMessage = () => {},
  onReconnect = () => {}, scheduler = (fn, ms) => setTimeout(fn, ms),
  unscheduler = (id) => clearTimeout(id),
  backoff = { base: 1000, max: 30000 }, heartbeat = DEFAULT_HEARTBEAT,
  // uni 的 SocketTask.send() 只认 `send({data})`（h5 与 App 运行时都是先取 `.data`），
  // 直接把字符串丢给它会发出 undefined —— 鉴权首帧根本到不了服务端，真机上就是无限重连。
  // 裸 WebSocket 恰好相反，只认字符串。
  objectPayload = Boolean(globalThis.uni?.connectSocket),
} = {}) {
  let attempts = 0;
  let closedByUser = false;
  let reconnecting = false;
  let beatTimer = null;
  let missed = 0;
  const state = { socket: null, connected: false };

  function send(obj) {
    const data = JSON.stringify(obj);
    state.socket?.send?.(objectPayload ? { data } : data);
  }

  function stopHeartbeat() {
    if (beatTimer !== null) unscheduler(beatTimer);
    beatTimer = null;
    missed = 0;
  }

  function scheduleBeat() {
    beatTimer = scheduler(() => {
      beatTimer = null;
      if (!state.connected) return;
      if (missed >= heartbeat.tolerateMissed) { dropAsStale(); return; }
      missed += 1;
      send({ type: "ping" });
      scheduleBeat();
    }, heartbeat.interval);
  }

  // 半开连接（隧道悄悄断了但本地 socket 还“开着”）不会自己触发 onclose，只能自己拆
  function dropAsStale() {
    stopHeartbeat();
    const sock = state.socket;
    state.socket = null;                 // 先摘掉，这只旧 socket 稍后的 onclose 就不会再排一次重连
    state.connected = false;
    sock?.close?.();
    scheduleReconnect();
  }

  function handleFrame(ev) {
    let msg;
    try { msg = JSON.parse(ev?.data); } catch { return; }
    if (msg.type === "hello") {
      onReconnect({ sinceId: msg.latest_inbox_id ?? null, unreadCount: msg.unread_count ?? 0 });
      return;
    }
    if (msg.type === "pong") { missed = 0; return; }
    if (msg.type === "msg") { onMessage(msg); send({ type: "pong" }); return; }
    if (msg.type === "ping") send({ type: "pong" });
  }

  function bind(sock) {
    const on = (web, uniEvt, handler) => {
      if (typeof sock[web] !== "undefined") sock[web] = handler;
      else if (sock[uniEvt]) sock[uniEvt](handler);
    };
    on("onopen", "onOpen", () => {
      if (state.socket !== sock) return;     // 已被更新的连接取代
      attempts = 0;
      state.connected = true;
      send({ type: "auth", token });
      stopHeartbeat();
      scheduleBeat();
    });
    on("onmessage", "onMessage", (ev) => {
      if (state.socket === sock) handleFrame(ev);
    });
    on("onclose", "onClose", () => {
      if (state.socket !== sock) return;
      state.connected = false;
      stopHeartbeat();
      scheduleReconnect();
    });
  }

  function scheduleReconnect() {
    if (closedByUser || reconnecting) return;
    reconnecting = true;
    const exp = Math.min(backoff.base * 2 ** attempts, backoff.max);
    const jitter = exp * (0.5 + Math.random() * 0.5);
    attempts += 1;
    scheduler(() => { reconnecting = false; if (!closedByUser) connect(); }, Math.round(jitter));
  }

  function connect() {
    const sock = socketFactory(url);
    state.socket = sock;
    bind(sock);
    return sock;
  }

  return {
    connect,
    close() { closedByUser = true; stopHeartbeat(); state.connected = false; state.socket?.close?.(); },
    get socket() { return state.socket; },
    get _socket() { return state.socket; },
    get connected() { return state.connected; }
  };
}
