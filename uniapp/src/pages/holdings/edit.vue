<!-- uniapp/src/pages/holdings/edit.vue -->
<script setup>
import { ref } from "vue";
import { onLoad } from "@dcloudio/uni-app";
import { useClient } from "../../composables/useClient.js";

const code = ref(""); const name = ref(""); const quantity = ref(""); const costPrice = ref("");
const err = ref("");
const { holdings } = useClient();
onLoad((q) => { if (q.code) code.value = q.code; });

async function save() {
  err.value = "";
  const body = { code: code.value, name: name.value, quantity: Number(quantity.value),
                 cost_price: Number(costPrice.value) };
  try {
    await holdings.create(body);
    uni.navigateBack();
  } catch (e) { err.value = e.message; }
}
</script>
<template>
  <view class="p">
    <input v-model="code" placeholder="6位代码" />
    <input v-model="name" placeholder="名称" />
    <input v-model="quantity" type="number" placeholder="数量" />
    <input v-model="costPrice" type="digit" placeholder="成本价" />
    <button :disabled="!code || !quantity || !costPrice" @click="save">保存</button>
    <text v-if="err" class="err">{{ err }}</text>
  </view>
</template>
