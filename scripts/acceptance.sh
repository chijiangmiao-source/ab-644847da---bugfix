#!/usr/bin/env bash
# 端到端自动化验收（需本机 Docker + Compose v2）：
#
#   ./scripts/acceptance.sh
#
# 流程：
#   1. compose 构建镜像并启动 web，等待 /health 健康；
#   2. 向 audit-data 卷预置修复部署前被错误冻结的旧记录（seed_legacy.py）；
#   3. 通过真实审计 API（compose 网络内 http://web:8080）执行
#      scripts/acceptance_checks.py：
#        live    —— 先兼容后不兼容两份独立审计、相反顺序、相同契约重传、
#                   按标识重开、不同契约 409；
#        legacy  —— 旧误判冻结记录按冻结原始契约恢复，保留标识/指纹/冻结时间；
#   4. restart web 后执行 persist —— 重启后读取全部结论保持稳定；
#   5. 运行既有 verify：递归兼容/字段缺失/额外变体复核、pytest、
#      字节码构建检查、审计接口冒烟。
#
# 任一步失败即非零退出。HOST_PORT（默认 8080）可用于宿主访问。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  echo "ERROR: 未找到 docker compose / docker-compose" >&2
  exit 127
fi

export HOST_PORT="${HOST_PORT:-8080}"
STATE_IN_VOLUME="/data/acceptance-state.json"

# 一次性容器：复用 web 镜像与 audit-data 卷，在 compose 网络内对真实 API 验收。
run_one_off() {
  "${COMPOSE[@]}" run --rm --no-deps -T web "$@"
}

cleanup() {
  echo
  echo "[acceptance] 停止 compose（保留 audit-data 卷；加 -v 可清除）"
  "${COMPOSE[@]}" down --remove-orphans || true
}
trap cleanup EXIT

echo "== [1/5] 构建镜像 =="
"${COMPOSE[@]}" build web verify

echo "== [2/5] 启动 web 并等待健康接口 =="
"${COMPOSE[@]}" up -d web
for _ in $(seq 1 30); do
  status="$(run_one_off python -c \
    'import json,urllib.request; print(json.load(urllib.request.urlopen("http://web:8080/health", timeout=2))["status"])' 2>/dev/null || true)"
  if [ "${status}" = "ok" ]; then
    echo "/health -> ok"
    break
  fi
  sleep 1
done
[ "${status:-}" = "ok" ] || { echo "ERROR: web 健康检查未通过" >&2; exit 1; }

echo "== [3/5] 预置修复部署前的旧冻结记录 =="
run_one_off python scripts/seed_legacy.py

echo "== [4/5] 真实审计 API 验收（live + legacy） =="
run_one_off python scripts/acceptance_checks.py live http://web:8080 --state "${STATE_IN_VOLUME}"
run_one_off python scripts/acceptance_checks.py legacy http://web:8080

echo "== [5/5] 重启服务后复核持久化，再运行既有 verify =="
"${COMPOSE[@]}" restart web
for _ in $(seq 1 30); do
  status="$(run_one_off python -c \
    'import json,urllib.request; print(json.load(urllib.request.urlopen("http://web:8080/health", timeout=2))["status"])' 2>/dev/null || true)"
  [ "${status:-}" = "ok" ] && { echo "/health -> ok（重启后）"; break; }
  sleep 1
done
[ "${status:-}" = "ok" ] || { echo "ERROR: 重启后健康检查未通过" >&2; exit 1; }

run_one_off python scripts/acceptance_checks.py persist http://web:8080 --state "${STATE_IN_VOLUME}"
"${COMPOSE[@]}" run --rm -T verify

echo
echo "ACCEPTANCE OK：审计隔离、违约定位、冻结修复、重启持久化、既有测试与构建检查全部通过"
