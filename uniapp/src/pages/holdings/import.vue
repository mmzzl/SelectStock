<!-- uniapp/src/pages/holdings/import.vue —— 交割单导入：粘贴 / 选文件 → 预览行级校验 → 提交 -->
<script setup>
import { ref, computed } from "vue";
import { useClient } from "../../composables/useClient.js";
import { toPreviewRow } from "../../logic/view.js";

const ACCEPT = [".csv", ".xlsx", ".pdf"];
const mode = ref("text");            // text | file
const text = ref("");
const picked = ref(null);            // { path, name, size } App 用 path；H5 用 file
const fileHandle = ref(null);        // H5: File 对象；App: 路径字符串
const pdfPwd = ref("");              // 加密对账单的打开密码：只在本次预览请求里用，不落任何存储
const preview = ref(null);           // { total, valid, errors:[{line,reason}], items:[] }
const previewing = ref(false);       // 预览解析进行中
const submitting = ref(false);
const resultMsg = ref("");
const err = ref("");
const diag = ref("");                // App 端读文件的逐级诊断：出错时显示出来，截图就能定位
const { trades } = useClient();

const canPreview = computed(() => (mode.value === "text" ? !!text.value.trim() : !!fileHandle.value) && !previewing.value);
// 一份对账单动辄两百行，全渲染会把「确认提交」推到屏幕外——列表只当抽样看，提交仍用 preview.items 全量
const shownRows = computed(() => (preview.value ? preview.value.viewRows.slice(0, 20) : []));
const pickedLabel = computed(() => {
  if (!picked.value) return "";
  const s = picked.value.size ? ` · ${(picked.value.size / 1024).toFixed(0)} KB` : "";
  return `${picked.value.name}${s}`;
});

function reset() { preview.value = null; resultMsg.value = ""; err.value = ""; diag.value = ""; }

// 解析要走完「读文件 → 上行 → pdf 解析」，真机两三秒起，没有状态就看着像按钮坏了
async function doPreview() {
  if (previewing.value) return;
  reset();
  previewing.value = true;
  uni.showLoading({ title: "解析中…", mask: true });
  try {
    let out;
    if (mode.value === "text") {
      out = await trades.previewText(text.value);
    } else {
      // #ifdef APP-PLUS
      out = await previewApp();
      // #endif
      // #ifndef APP-PLUS
      out = await trades.previewFile(fileHandle.value, "file", pdfPwd.value.trim());
      // #endif
    }
    preview.value = { ...out, viewRows: (out.items || []).map(toPreviewRow) };
    if (!preview.value.viewRows.length) err.value = "没有解析出有效行，请检查列名或格式";
  } catch (e) { err.value = e.message; } finally {
    previewing.value = false;
    uni.hideLoading();
  }
}

// #ifdef H5
function pickH5() {
  // 模板里写 <input type="file"> 会被 uni-app 换成 uni-input，选择按钮被样式吃掉变成空白框；
  // 这里临时建原生 input，点按钮直接弹系统文件对话框。
  const el = document.createElement("input");
  el.type = "file";
  el.accept = ACCEPT.join(",");
  el.onchange = () => {
    const f = el.files && el.files[0];
    if (!f) return;
    picked.value = { name: f.name, size: f.size };
    fileHandle.value = f;        // H5 走 uni.uploadFile 的 file 参数
    reset();
  };
  el.click();
}
// #endif

// #ifdef APP-PLUS
const permNote = ref("");            // 存储权限申请结果，只作为诊断证据拼进 probe

function displayName(p) {
  // SAF 的路径末段是 document:141169 这种文档句柄，不是文件名；向 ContentResolver 问真实显示名。
  // 问不到就返回空，由调用方退化成通用文案——绝不能因为拿不到名字就拒绝这个文件。
  try {
    const Uri = plus.android.importClass("android.net.Uri");
    const resolver = plus.android.invoke(plus.android.runtimeMainActivity(), "getContentResolver");
    const cursor = plus.android.invoke(resolver, "query", Uri.parse(p), null, null, null, null);
    if (!cursor) return "";
    plus.android.importClass(cursor);
    let name = "";
    if (cursor.moveToFirst()) {
      const i = cursor.getColumnIndex("_display_name");
      if (i >= 0) name = String(cursor.getString(i) || "");
    }
    cursor.close();
    return name;
  } catch (e) { return ""; }
}

function errText(e) {
  if (!e) return "?";
  return String(e.code || e.message || e.errMsg || JSON.stringify(e));
}

// SAF 只给 content:// 句柄，uni.uploadFile 读它会把整个文件段静默丢掉（服务端只收到 password，
// 表现就是「没收到文件」）。所以先在预览这一刻逐级尝试读取，每级结果都记进 diag——
// 绝不再悄悄退回原句柄：那等于把同一个 400 重复一遍，还看不出卡在哪。
function resolveRealPath(src, saveName, cb) {
  let done = false;
  const finish = (p, how) => { if (!done) { done = true; cb(p || "", how); } };
  try {
    plus.io.resolveLocalFileSystemURL(src, (entry) => {
      const local = String((entry.toLocalURL && entry.toLocalURL()) || "");
      if (local && !/^content:/i.test(local)) { finish(local, "resolved"); return; }
      plus.io.requestFileSystem(plus.io.PRIVATE_DOC, (fs) => {
        entry.copyTo(fs.root, saveName,
          (copied) => finish(String(copied.toLocalURL() || copied.fullPath || ""), "copied-to-_doc"),
          (e) => finish("", "copyTo失败:" + errText(e)));
      }, (e) => finish("", "requestFileSystem失败:" + errText(e)));
    }, (e) => finish("", "resolve失败:" + errText(e)));
  } catch (e) { finish("", "resolve抛错:" + errText(e)); }
}

function readB64ByIo(src, cb) {
  let done = false;
  const finish = (b, how) => { if (!done) { done = true; cb(b || "", how); } };
  try {
    plus.io.resolveLocalFileSystemURL(src, (entry) => {
      entry.file((f) => {
        const r = new plus.io.FileReader();
        r.onloadend = (ev) => {
          const s = String((ev && ev.target && ev.target.result) || r.result || "");
          finish(s.includes(",") ? s.slice(s.indexOf(",") + 1) : "", `io-read size=${f.size || "?"}`);
        };
        r.onerror = (e) => finish("", "FileReader失败:" + errText(e));
        r.readAsDataURL(f);
      }, (e) => finish("", "entry.file失败:" + errText(e)));
    }, (e) => finish("", "io-read resolve失败:" + errText(e)));
  } catch (e) { finish("", "io-read抛错:" + errText(e)); }
}

// 最后一级：直接走 ContentResolver 输入流，绕开 plus.io 与 uploadFile。
// 块长取 3 的倍数 → NO_WRAP 编码后恰为 4 的整数倍，base64 串才能首尾相接；不足整块即读到末尾。
function b64FromStream(ins, tag, cb) {
  try {
    const Base64 = plus.android.importClass("android.util.Base64");
    plus.android.importClass(ins);
    const CHUNK = 24576, FULL = 32768;
    let out = "", guard = 0;
    while (guard++ < 1200) {                 // 1200×24KB ≈ 28MB，覆盖服务端 20MB 上限
      const piece = String(Base64.encodeToString(plus.android.invoke(ins, "readNBytes", CHUNK), 2) || "");
      if (!piece) break;
      out += piece;
      if (piece.length < FULL) break;
    }
    plus.android.invoke(ins, "close");
    cb(out, `${tag} b64_chars=${out.length}`);
  } catch (e) { cb("", `${tag}抛错:` + errText(e)); }
}

function readB64ByNative(src, cb) {
  try {
    const Uri = plus.android.importClass("android.net.Uri");
    const resolver = plus.android.invoke(plus.android.runtimeMainActivity(), "getContentResolver");
    const ins = plus.android.invoke(resolver, "openInputStream", Uri.parse(src));
    if (!ins) { cb("", "openInputStream=null"); return; }
    b64FromStream(ins, "原生读", cb);
  } catch (e) { cb("", "原生读抛错:" + errText(e)); }
}

// plus.io 给的可能是 file:// URL，也可能是 _doc/ 这类相对别名；原生 File 只吃绝对路径。
function absOf(p) {
  const s = String(p || "");
  if (/^file:\/\//i.test(s)) {
    try { return decodeURIComponent(s.replace(/^file:\/\/(localhost)?/i, "")); }
    catch (e) { return s.replace(/^file:\/\/(localhost)?/i, ""); }
  }
  try {
    const c = String(plus.io.convertLocalFileSystemURL(s) || "");
    if (c.charAt(0) === "/") return c;
  } catch (e) { /* 留着原值，让调用方的错误分支去说清楚 */ }
  return s;
}

// 这个路径到底读得到读不到，是判断「容器/分区存储拦路」还是「uploadFile 自己丢段」的唯一证据，
// 所以无论成功失败都要把它拼进诊断串上行。
function probeFile(abs) {
  try {
    const f = plus.android.newObject("java.io.File", abs);
    return `exists=${plus.android.invoke(f, "exists")} canRead=${plus.android.invoke(f, "canRead")} bytes=${plus.android.invoke(f, "length")}`;
  } catch (e) { return "探测抛错:" + errText(e); }
}

function readB64ByPath(abs, cb) {
  try {
    const ins = plus.android.newObject("java.io.FileInputStream", abs);
    if (!ins) { cb("", "FileInputStream=null"); return; }
    b64FromStream(ins, "路径读", cb);
  } catch (e) { cb("", "FileInputStream抛错:" + errText(e)); }
}

// Native.js 把 content:// 的输入流直接写进 App 沙盒。
// 这一级不经过 plus.io 的 URL 解析，也不依赖 uni.uploadFile 认句柄——卓易通这类
// 「只给句柄、不给宿主真实路径」的容器环境里，它是唯一还能拿到真路径的办法。
function copyUriToDoc(src, rel, cb) {
  try {
    const abs = String(plus.io.convertLocalFileSystemURL(rel) || "");
    if (!abs || abs === rel) { cb("", "convertLocalFileSystemURL无绝对路径"); return; }
    const Uri = plus.android.importClass("android.net.Uri");
    const main = plus.android.runtimeMainActivity();
    const ins = plus.android.invoke(plus.android.invoke(main, "getContentResolver"),
      "openInputStream", Uri.parse(src));
    if (!ins) { cb("", "openInputStream=null"); return; }
    plus.android.importClass(ins);
    const byteCls = plus.android.classType ? plus.android.classType("byte")
      : plus.android.importClass("java.lang.Byte").TYPE;
    const buf = plus.android.importClass("java.lang.reflect.Array").newInstance(byteCls, 65536);
    const out = plus.android.newObject("java.io.FileOutputStream",
      plus.android.newObject("java.io.File", abs));
    plus.android.importClass(out);
    let total = 0, guard = 0;
    while (guard++ < 4000) {                     // 4000×64KB，远覆盖服务端 20MB 上限
      const n = Number(plus.android.invoke(ins, "read", buf));
      if (!n || n <= 0) break;
      plus.android.invoke(out, "write", buf, 0, n);
      total += n;
    }
    plus.android.invoke(out, "close");
    plus.android.invoke(ins, "close");
    cb(total > 0 ? rel : "", `native-copy bytes=${total}`);
  } catch (e) { cb("", "原生拷贝抛错:" + errText(e)); }
}

// 容器里的文件读取可能落在传统存储上；被拒不拦流程，只记进诊断串当证据
function askStoragePerm(cb) {
  try {
    if (!plus.android || typeof plus.android.requestPermissions !== "function") { cb("无requestPermissions"); return; }
    plus.android.requestPermissions(["android.permission.READ_EXTERNAL_STORAGE"],
      (r) => cb(`granted=${((r && r.granted) || []).length} denied=${(((r && r.deniedAlways) || []).length + (((r && r.deniedPresent) || []).length))}`),
      (e) => cb("权限请求失败:" + errText(e)));
  } catch (e) { cb("权限抛错:" + errText(e)); }
}

// 只要有一个能用的路径，就先把字节自己读出来。
async function bytesFromPath(p, steps) {
  const abs = absOf(p);
  const note = probeFile(abs);
  steps.push(`探测(${note})`);
  const byJava = await new Promise((res) => readB64ByPath(abs, (b, how) => { steps.push(how); res(b); }));
  if (byJava) return { b64: byJava, how: `路径读 ${note}` };
  const byIo = await new Promise((res) => readB64ByIo(p, (b, how) => { steps.push(how); res(b); }));
  if (byIo) return { b64: byIo, how: `io读 ${note}` };
  return null;
}

async function previewApp() {
  const src = String(fileHandle.value || "");
  const password = pdfPwd.value.trim();
  const name = (picked.value && picked.value.name) || "";
  const steps = [];
  const saveName = `${Date.now()}_deal`;
  const perm = permNote.value ? ` 存储权限=${permNote.value}` : "";
  // 真机证据（0.1.8）：plus.io 已经解析出 file:///storage/emulated/0/… 这样的真路径，
  // 但把它交给 uni.uploadFile 后服务端仍然只收到 password+probe——这个容器里 uploadFile 会静默丢文件段。
  // 所以 base64 是首选通道，uploadFile 只在所有读取手段都失败后才试一次（它至少能在日志里再证明一次）。
  const send = (got) => {
    if (!got) return null;
    diag.value = `通道=base64(${got.how})${perm}`;
    return trades.previewBase64(got.b64, name, password, diag.value);
  };

  const local = await new Promise((res) =>
    resolveRealPath(src, saveName, (p, how) => { steps.push(how); res(p); }));
  if (local) {
    const got = await bytesFromPath(local, steps);
    const out = send(got);
    if (out) return out;
  }

  const copied = await new Promise((res) =>
    copyUriToDoc(src, `_doc/${saveName}`, (p, how) => { steps.push(how); res(p); }));
  if (copied) {
    const got = await bytesFromPath(copied, steps);
    const out = send(got);
    if (out) return out;
    diag.value = `通道=multipart(native-copy) path=${copied} 前置=${steps.join("|")}${perm}`;
    return await trades.previewFile(copied, "file", password, diag.value);
  }

  if (local) {
    diag.value = `通道=multipart(resolved-fallback) path=${local} 前置=${steps.join("|")}${perm}`;
    return await trades.previewFile(local, "file", password, diag.value);
  }

  const read = await new Promise((res) => readB64ByIo(src, (b64, how) => {
    steps.push(how);
    if (b64) return res({ b64, how });
    readB64ByNative(src, (b2, how2) => { steps.push(how2); res({ b64: b2, how: how2 }); });
  }));
  if (!read.b64) {
    throw new Error(`App 没能读出这个文件（src=${src.slice(0, 48)} ${steps.join(" → ")}${perm}）。`
      + "鸿蒙卓易通下请先把文件用「卓易通→文件互传」导入容器再选，或改用「粘贴文本」导入");
  }
  diag.value = `通道=base64(${read.how})${perm}`;
  return await trades.previewBase64(read.b64, name, password, diag.value);
}

function pickAppFile() {
  reset();
  // App 端没有 input[type=file]，且 uni.chooseFile 在 uni-app-plus 运行时里没实现（只有 uni-h5 有），
  // 调它是静默 TypeError——表现就是按钮点了没反应。原生入口是 plus.io.chooseFile（内部 SAF OPEN_DOCUMENT，不要存储权限）。
  if (typeof plus === "undefined" || !plus.io || typeof plus.io.chooseFile !== "function") {
    err.value = "当前 App 运行库没有文件选择能力，请改用「粘贴文本」导入，或升级 HBuilderX 后重新出包";
    return;
  }
  plus.io.chooseFile(
    { title: "选择交割单", extension: ACCEPT, multiple: false },
    (res) => {
      const p = decodeURIComponent((res && res.files && res.files[0]) || "");
      if (!p) { err.value = "未选中文件"; return; }
      const cand = displayName(p) || p.split("/").pop();
      picked.value = { path: p, name: /\.\w+$/.test(cand) ? cand : "已选择文件（按内容识别格式）", size: 0 };
      fileHandle.value = p;      // 原句柄先存着，点「预览解析」时才决定用哪条通道读
      permNote.value = "";
      askStoragePerm((s) => { permNote.value = s; });
    },
    (e) => { err.value = "打开文件选择器失败：" + (errText(e) === "?" ? JSON.stringify(e || {}) : errText(e)); },
  );
}
// #endif

function clearPick() { picked.value = null; fileHandle.value = null; reset(); }

async function doCommit() {
  submitting.value = true;
  try {
    const res = await trades.commit(preview.value.items);
    if (res.errors?.length) {
      err.value = `提交失败 ${res.errors.length} 行：` + res.errors.map((x) => `第${x.line}行 ${x.reason}`).join("；");
      return;
    }
    const head = res.imported
      ? `导入 ${res.imported} 条；持仓变更: ${(res.holdings_changed || []).join(", ") || "无"}`
      : `没有新增成交：${res.duplicates || 0} 笔与库里已有记录完全重复`;
    resultMsg.value = head
      + ((res.warnings || []).length ? `；${res.warnings.join("；")}` : "")
      + "。回到「交割记录」「持仓列表」即可看到数据";
    preview.value = null;
    uni.showToast({ title: res.imported ? `导入 ${res.imported} 条` : "无新增（重复）" });
  } catch (e) { err.value = e.message; } finally { submitting.value = false; }
}
</script>
<template>
  <view class="p">
    <view class="seg">
      <button :class="{ on: mode === 'file' }" @click="mode = 'file'">上传文件</button>
      <button :class="{ on: mode === 'text' }" @click="mode = 'text'">粘贴文本</button>
    </view>

    <textarea v-if="mode === 'text'" v-model="text"
      placeholder="粘贴券商交割单：逗号、制表符、空格对齐都认；在 PDF 阅读器里全选复制过来也能解析" />

    <view v-else class="form">
      <text class="reason">支持 csv / xlsx / pdf（PDF 需带可选文字表格，扫描件不支持；加密 PDF 在下方填打开密码）</text>
      <!-- #ifdef H5 -->
      <button @click="pickH5">选择交割单文件</button>
      <!-- #endif -->
      <!-- #ifdef APP-PLUS -->
      <button @click="pickAppFile">选择交割单文件</button>
      <!-- #endif -->
      <view v-if="picked" class="picked">
        <text class="pname">{{ pickedLabel }}</text>
        <text class="clr" @click="clearPick">清除</text>
      </view>
      <view class="fld">
        <text class="lab">PDF 打开密码（加密对账单才需要，csv/xlsx 留空）</text>
        <input v-model="pdfPwd" password placeholder="东财等通常是身份证后 6 位或资金账号后 6 位" />
      </view>
    </view>

    <button class="btn-big" :disabled="!canPreview" @click="doPreview">
      {{ previewing ? "解析中…" : "预览解析" }}
    </button>
    <text v-if="err" class="err">{{ err }}</text>
    <!-- #ifdef APP-PLUS -->
    <text v-if="diag && err" class="reason">诊断：{{ diag }}</text>
    <!-- #endif -->

    <view v-if="preview">
      <view class="bar">
        <text>共 {{ preview.total }} 行</text>
        <text class="ok-in">有效 {{ preview.valid }}</text>
        <text :class="(preview.errors || []).length ? 'bad-in' : ''">错误 {{ (preview.errors || []).length }}</text>
        <text v-if="preview.skipped" class="reason">已忽略 {{ preview.skipped }} 行非买卖流水</text>
      </view>
      <!-- 提交按钮必须出现在首屏：真机上 180 行列表把按钮顶到屏幕外，用户以为「解析完自动就存了」 -->
      <button class="btn-big" :disabled="!preview.viewRows.length || submitting" @click="doCommit">
        {{ submitting ? "提交中…" : `确认提交 ${preview.viewRows.length} 笔` }}
      </button>
      <view v-for="e in preview.errors" :key="e.line" class="err">第 {{ e.line }} 行：{{ e.reason }}</view>
      <view class="sec">明细（显示前 {{ shownRows.length }} 笔，提交为全部 {{ preview.viewRows.length }} 笔）</view>
      <view v-for="(r, i) in shownRows" :key="i" class="trow">
        <view class="l">
          <text class="d">{{ r.date }}</text>
          <text class="n">{{ r.name }}（{{ r.code }}）</text>
        </view>
        <view class="r">
          <text class="badge" :class="r.direction">{{ r.directionText }}</text>
          <text class="q">{{ r.quantity }} @ {{ r.price }}</text>
        </view>
      </view>
    </view>
    <text v-if="resultMsg" class="ok">{{ resultMsg }}</text>
  </view>
</template>
<style scoped>
.seg { display: flex; gap: 16rpx; }
.seg button { flex: 1; background: #fff; color: #5c6470; border: 2rpx solid #e2e6ef; }
.seg button.on { background: #2b5cff; color: #fff; border-color: #2b5cff; }
textarea {
  width: 100%; min-height: 220rpx; background: #fff; border-radius: 16rpx;
  padding: 22rpx; box-sizing: border-box; box-shadow: 0 4rpx 18rpx rgba(31, 35, 41, 0.06);
}
.picked {
  display: flex; align-items: center; justify-content: space-between;
  margin-top: 14rpx; padding: 16rpx 20rpx; background: #eef3ff; border-radius: 12rpx;
}
.picked .pname { font-size: 26rpx; color: #2b5cff; }
.picked .clr { font-size: 24rpx; color: #8a919f; padding: 0 10rpx; }
.fld { margin-top: 14rpx; }
.lab { display: block; font-size: 22rpx; color: #8a919f; margin-top: 8rpx; }
.bar text { margin: 0; }
.ok-in { color: #1fa35f; }
.bad-in { color: #e64545; }
</style>
