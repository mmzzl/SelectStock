#!/usr/bin/env bash
# 卸载服务（不删除代码和数据）
set -euo pipefail
APP_NAME="fastapi-template"
if [ "$(id -u)" -ne 0 ]; then
  echo "请用 sudo 运行: sudo bash uninstall.sh" >&2
  exit 1
fi
systemctl stop ${APP_NAME} 2>/dev/null || true
systemctl disable ${APP_NAME} 2>/dev/null || true
rm -f /etc/systemd/system/${APP_NAME}.service
systemctl daemon-reload
echo "服务已卸载。代码仍在安装目录，如需删除请手动执行 rm -rf <安装目录>"
