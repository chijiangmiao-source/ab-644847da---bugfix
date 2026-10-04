#!/usr/bin/env bash
# 端到端验收：Compose 构建并启动服务 -> 健康确认 -> 真实审计 API 隔离场景
# -> 服务重启后的冻结结论读取 -> 现有代码测试与构建检查（verify 组件内含）。
#
# 用法：
#   scripts/acceptance.sh            # 完整验收
#   HOST_PORT=18080 scripts/acceptance.sh
set -euo pipefail
cd "$(dirname "$0")/.."

TOKEN="${AUDIT_RUN_TOKEN:-acc-$(date +%s%N)}"

echo "==> [1/4] 构建并启动服务（AUDIT_RUN_TOKEN=$TOKEN）"
docker compose up -d --build web

echo "==> [2/4] 运行验收组件：复核 -> pytest -> 构建检查 -> 接口冒烟 -> 审计隔离场景"
# verify 依赖 web 健康（depends_on: service_healthy），通过真实审计 API
# 连续提交两份同名递归契约的独立审计并核对拒绝路径与违约类别。
docker compose run --rm -e AUDIT_RUN_TOKEN="$TOKEN" verify

echo "==> [3/4] 重启服务，复核冻结结论在重启后的读取"
docker compose restart web
docker compose run --rm \
  -e AUDIT_RUN_TOKEN="$TOKEN" \
  -e AUDIT_PHASE=recheck \
  verify

echo "==> [4/4] ACCEPTANCE OK：隔离、重传、重开、重启后读取与回归检查全部通过"
