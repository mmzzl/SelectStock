<!-- uniapp/src/pages/holdings/import.vue -->
<script setup>
import { ref } from "vue";
import { useClient } from "../../composables/useClient.js";
import { toPreviewRow } from "../../logic/view.js";

const mode = ref("text");          // text | file
const text = ref("");
const pickedPath = ref(null);
const preview = ref(null);          // { total, valid, errors:[{line,reason}], items:[] }
const submitting = ref(false);
const resultMsg = ref("");
const { trades } = useClient();

async function doPreview() {
  preview.value = null; resultMsg.value = ""; err.value = "";
  try {
    const out = mode.value === "text"
      ? await trades.previewText(text.value)
      : await trades.previewFile(pickedPath.value, "file");
    preview.value = { ...out, viewRows: (out.items || []).map(toPreviewRow) };
  } catch (e) { err.value = e.message; }
}
const err = ref("");
function onPick(e) {
  const f = e.target?.files?.[0];
  if (f) { pickedPath.value = f; }   // H5: uni.uploadFile 支持 Blob/File；App: filePath 字符串
}
async function doCommit() {
  submitting.value = true;
  try {
    const res = await trades.commit(preview.value.items);
    if (res.errors?.length) {
      err.value = `提交失败 ${res.errors.length} 行：` + res.errors.map((x) => `第${x.line}行 ${x.reason}`).join("；");
      return;
    }
    resultMsg.value = `导入 ${res.imported} 条；持仓变更: ${(res.holdings_changed || []).join(", ") || "无"}`;
    preview.value = null;
    uni.showToast({ title: `导入 ${res.imported} 条` });
  } catch (e) { err.value = e.message; } finally { submitting.value = false; }
}
</script>
<template>
  <view class="p">
    <view class="seg">
      <button :class="{ on: mode === 'file' }" @click="mode = 'file'">上传文件</button>
      <button :class="{ on: mode === 'text' }" @click="mode = 'text'">粘贴 CSV</button>
    </view>
    <textarea v-if="mode === 'text'" v-model="text" placeholder="粘贴券商交割单（成交日期,证券代码,…）" />
    <input v-else type="file" accept=".csv,.xlsx" @change="onPick" />
    <button :disabled="mode === 'text' ? !text : !pickedPath" @click="doPreview">预览解析</button>
    <text v-if="err" class="err">{{ err }}</text>

    <view v-if="preview">
      <view class="bar">共 {{ preview.total }} 行，有效 {{ preview.valid }}，错误 {{ (preview.errors || []).length }}</view>
      <view v-for="e in preview.errors" :key="e.line" class="err">第 {{ e.line }} 行：{{ e.reason }}</view>
      <view v-for="r in preview.viewRows" :key="r.date + r.code + r.direction + r.quantity" class="card">
        {{ r.date }} {{ r.name }}({{ r.code }}) {{ r.directionText }} {{ r.quantity }} @{{ r.price }}
      </view>
      <button :disabled="!preview.viewRows.length || submitting" @click="doCommit">确认提交</button>
    </view>
    <text v-if="resultMsg" class="ok">{{ resultMsg }}</text>
  </view>
</template>
