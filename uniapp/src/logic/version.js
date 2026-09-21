// uniapp/src/logic/version.js —— 版本比对纯函数（R1-3 允许的元信息判定）
export function checkUpdate({ local, remote }) {
  if (!remote) return { hasUpdate: false, latest: null, forced: false, packageType: "" };
  const rc = Number(remote.version_code) || 0;
  return {
    hasUpdate: rc > Number(local || 0),
    latest: remote,
    forced: Boolean(remote.forced),
    packageType: remote.package_type || "",
  };
}
