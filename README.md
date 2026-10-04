# 星载递归载荷兼容审计

在升级星载指令发送端、重新定义载荷之前，确认**新版发送端可能产生的每一种递归载荷**
都能被在轨接收端接受。系统以“发送端是生产者、接收端是消费者”裁决
**协归（coinductive）结构子类型**，结论按稳定审计标识冻结。

## 它保证什么

- **精确的生产者 ⪯ 消费者结构子类型**
  - 接收端要求的必需字段必须由发送端以必需字段提供；接收端可选字段缺失合法，
    存在则值类型相容；发送端多余字段合法。
  - 发送端可能发出的每个变体标签都必须在接收端标签集合中，载荷类型相容；
    接收端多余标签合法。
  - 基本类型 `int / bool / text` 仅与自身相容。
- **递归以协归关系裁决**：沿具名引用展开，环上的比较对以“假设-复用”收口，
  **不展开到固定深度，也不用抽样实例代替**；每个被复用的递归比较对都会在结论中标出。
- **稳定首个违约**：字段按接收端声明顺序、标签按发送端声明顺序遍历，
  返回确定性的首个 `字段缺失 / 可空性变化 / 额外标签 / 基本类型违约`，并给出类型路径。
- **契约问题一次反馈**：未定义引用、重复类型名/字段/标签、无保护别名环、
  不合法标识、超过 24 个类型等，一次性全部返回。
- **结论冻结**：相同 `audit_id` 重传相同契约读取原结论（`frozen_at` 不变）；
  改变任一契约返回 `409` 且绝不改写原结论。
- **审计彼此隔离**：每次裁决使用全新的递归比较对缓存，一份审计的递归裁决
  绝不影响另一份稳定审计标识下的结论——同名、同递归拓扑但定义不同的契约
  在全新审计标识下独立裁决。在隔离修复之前被误判冻结的历史记录，按原审计
  标识重开或以完全相同契约重传时，依据其冻结的原始契约恢复正确结论，
  同时保留审计标识、契约指纹与冻结时间。

## 类型模型（JSON）

```json
{"kind":"int"} | {"kind":"bool"} | {"kind":"text"}
{"kind":"record","fields":[{"name":"x","type":Type,"required":true}]}
{"kind":"variant","tags":[{"label":"A","type":Type}]}
{"kind":"ref","name":"TypeName"}
```

每套声明为列表 `[{"name": "...", "type": Type}, ...]`（至多 24 个）。
“无保护别名环”指仅由 `ref`/别名构成、不经过任何 `record`/`variant`
受保护节点的环（如 `A = ref B; B = ref A`），这类类型没有确定结构，拒绝。
受保护递归（如 `List = variant{Nil, Cons{head, tail: List}}`）合法。

## 启动（容器化）

```bash
# 默认宿主端口 8080；可用 HOST_PORT 配置
HOST_PORT=18080 docker compose up -d --build
curl -s http://localhost:18080/health      # {"status":"ok"}
# 浏览器打开 http://localhost:18080/ 提交/重开结论
```

结论持久化在命名卷 `audit-data`，容器重启后仍可按标识重开。

## 单次验收组件 verify

`verify` 服务只运行一次并以退出码报告（复核递归兼容/字段缺失/额外变体 →
pytest → 构建检查 → 审计接口冒烟 → 审计隔离场景）：

```bash
docker compose build verify
docker compose run --rm verify      # 退出码 0 即验收通过
```

## 端到端验收 acceptance

`scripts/acceptance.sh` 编排完整验收：Compose 构建并启动服务、确认健康后，
通过真实审计 API 连续提交两份同名递归契约的独立审计（第二份在载荷 `Data`
序号处要求文本、必须稳定拒绝），覆盖相反提交顺序、相同契约重传、按标识
重开，随后重启服务复核冻结结论的读取：

```bash
scripts/acceptance.sh               # 退出码 0 即验收通过
```

## 本地开发

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q
.venv/bin/python scripts/verify.py                       # 不依赖 docker 的完整验收
AUDIT_DATA_DIR=./data PORT=8080 .venv/bin/python -m app.main
```

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/api/audits` | 提交或幂等重传；新建 `201`、相同契约重传 `200`、契约冲突 `409`、契约非法 `400`（`issues[]` 一次返回） |
| GET | `/api/audits/{audit_id}` | 重开冻结结论，不存在返回 `404` |

提交体：

```json
{
  "audit_id": "CMD-AUDIT-2026-001",
  "root_name": "Cmd",
  "sender_types": [ /* 发送端具名类型声明 */ ],
  "receiver_types": [ /* 接收端具名类型声明 */ ]
}
```

发送端只有一个类型时 `root_name` 可省略；否则必须在两套声明中都存在。

## 目录

- `app/models.py` — 领域模型、违约/复用点/结论
- `app/parser.py` — JSON 解析与全部静态校验（问题一次反馈）
- `app/subtype.py` — 协归结构子类型引擎
- `app/storage.py` — 契约指纹（SHA-256）、冻结/幂等/冲突
- `app/api.py`, `app/main.py` — HTTP API 与入口
- `app/static/` — 提交与重开页面（真实 API）
- `scripts/verify.py` — 单次验收组件
- `scripts/acceptance.sh` — 端到端验收（Compose 编排，含服务重启复核）
- `tests/` — 44 项测试（递归、互递归、稳定性、冻结、审计隔离、API）
