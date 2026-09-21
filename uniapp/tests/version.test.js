// uniapp/tests/version.test.js
import test from "node:test";
import assert from "node:assert/strict";
import { checkUpdate } from "../src/logic/version.js";

test("远程新于本地 → has_update true，且 forced 透传", () => {
  const info = checkUpdate({ local: 100, remote: { version_code: 102, forced: true } });
  assert.equal(info.hasUpdate, true);
  assert.equal(info.forced, true);
});

test("本地已最新 → has_update false", () => {
  assert.equal(checkUpdate({ local: 102, remote: { version_code: 102 } }).hasUpdate, false);
});

test("远程为空 → has_update false", () => {
  assert.equal(checkUpdate({ local: 100, remote: null }).hasUpdate, false);
});

test("H5 忽略下载包字段（无 url 也可提示刷新）", () => {
  const info = checkUpdate({ local: 100, remote: { version_code: 101, package_type: "h5" } });
  assert.equal(info.hasUpdate, true);
  assert.equal(info.packageType, "h5");
});
