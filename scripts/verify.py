#!/usr/bin/env python3
"""单次验收组件（verify）：

按顺序执行，任一步失败即以非零退出码报告：
  1. 复核递归兼容、字段缺失、额外发送变体、可空性变化等裁决（直连引擎），
     以及同名递归契约在冻结存储层的审计隔离；
  2. 代码测试（pytest）；
  3. 构建检查（全量字节码编译 + 关键模块导入）；
  4. 审计接口冒烟（/health、提交、幂等重传、契约冲突 409、重开）；
  5. 审计隔离场景（真实 API）：同名递归契约的两份独立审计互不影响，
     覆盖两种提交顺序、相同契约重传、契约冲突与按标识重开。

接口冒烟与隔离场景默认在进程内临时起服，并在同一数据目录上重启服务复核
冻结结论的读取；当设置环境变量 AUDIT_SMOKE_BASE_URL（如 compose 中指向
http://web:8080）时，直接对正在运行的容器执行。Compose 下“服务重启后的
读取”由 scripts/acceptance.sh 重启 web 后以 AUDIT_PHASE=recheck 并传入
同一 AUDIT_RUN_TOKEN 驱动本组件复核。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.api import build_server  # noqa: E402
from app.parser import parse_payload  # noqa: E402
from app.storage import AuditStore  # noqa: E402
from app.subtype import check_compatibility  # noqa: E402

INT = {"kind": "int"}
BOOL = {"kind": "bool"}
TEXT = {"kind": "text"}


def rec(*fields):
    return {"kind": "record", "fields": list(fields)}


def field(name, t, required=True):
    return {"name": name, "type": t, "required": required}


def variant(*tags):
    return {"kind": "variant", "tags": list(tags)}


def tag(label, t):
    return {"label": label, "type": t}


def ref(name):
    return {"kind": "ref", "name": name}


def isolation_contract(audit_id, receiver_seq):
    """同名、同递归拓扑的契约：根类型含必需载荷引用，载荷为带 Data 标签
    的递归类型；Data 含序号（发送端始终产生整数）和可选的自引用后继。
    receiver_seq 为接收端在 Data 序号处要求的类型：int 兼容、text 违约。
    """

    def decls(seq_type):
        return [
            {"name": "Cmd", "type": rec(field("payload", ref("Payload")))},
            {"name": "Payload", "type": variant(tag("Data", rec(
                field("seq", seq_type),
                field("next", ref("Payload"), required=False),
            )))},
        ]

    return {
        "audit_id": audit_id,
        "root_name": "Cmd",
        "sender_types": decls(INT),
        "receiver_types": decls(receiver_seq),
    }


ISO_MISMATCH_PATH = "Cmd.payload[Data].seq"
ISO_MISMATCH_CODE = "primitive-mismatch"


class Failure(Exception):
    pass


def check(step, condition, detail=""):
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {step}{(' — ' + detail) if detail else ''}")
    if not condition:
        raise Failure(step)


# ---------------------------------------------------------------- 阶段 1

def engine_verdict(body):
    p = parse_payload(body)
    ok, mismatch, recycled = check_compatibility(
        p.sender_types, p.receiver_types, p.root_name
    )
    return ok, mismatch, recycled


def phase_engine_recheck():
    print("[1/5] 复核递归兼容裁决核心结果")

    list_decl = [
        {"name": "List", "type": variant(
            tag("Nil", rec()),
            tag("Cons", rec(
                field("head", INT),
                field("tail", ref("List"), required=False),
            )),
        )}
    ]
    ok, _, recycled = engine_verdict({
        "audit_id": "verify-list", "root_name": "List",
        "sender_types": list_decl, "receiver_types": list_decl,
    })
    check("递归列表自相容（协归，无深度截断）", ok)
    check(
        "递归处标出已复用比较对 List <= List",
        any("List <= List" in e.pair for e in recycled),
        f"{len(recycled)} 个复用点",
    )

    ok, mismatch, _ = engine_verdict({
        "audit_id": "verify-missing",
        "sender_types": [{"name": "R", "type": rec(field("a", INT))}],
        "receiver_types": [{"name": "R", "type": rec(
            field("a", INT), field("b", TEXT))}],
    })
    check("接收端必需字段缺失被拒", not ok and mismatch.code == "field-missing")
    check("首个违约路径稳定为 R.b", mismatch.path == "R.b", mismatch.path)

    ok, mismatch, _ = engine_verdict({
        "audit_id": "verify-extra-tag",
        "sender_types": [{"name": "R", "type": variant(
            tag("A", INT), tag("B", INT))}],
        "receiver_types": [{"name": "R", "type": variant(tag("A", INT))}],
    })
    check("额外发送变体被拒", not ok and mismatch.code == "extra-tag")
    check("首个违约标签稳定为 R[B]", mismatch.path == "R[B]", mismatch.path)

    ok, mismatch, _ = engine_verdict({
        "audit_id": "verify-nullability",
        "sender_types": [{"name": "R", "type": rec(field("a", INT, False))}],
        "receiver_types": [{"name": "R", "type": rec(field("a", INT, True))}],
    })
    check("必需->可选可空性变化被拒",
          not ok and mismatch.code == "optional-required")

    # 深层递归类型不一致：int vs text，必须沿递归裁决到基本类型。
    s_list = [{"name": "List", "type": variant(
        tag("Nil", rec()),
        tag("Cons", rec(field("head", INT),
                        field("tail", ref("List"), False))),
    )}]
    r_list = [{"name": "List", "type": variant(
        tag("Nil", rec()),
        tag("Cons", rec(field("head", TEXT),
                        field("tail", ref("List"), False))),
    )}]
    ok, mismatch, _ = engine_verdict({
        "audit_id": "verify-deep", "root_name": "List",
        "sender_types": s_list, "receiver_types": r_list,
    })
    check("递归深层基本类型违约被发现",
          not ok and mismatch.code == "primitive-mismatch",
          getattr(mismatch, "path", ""),
    )

    # 审计隔离（冻结存储层）：同名递归契约的两份独立审计互不影响。
    # 回归背景：递归比较对缓存曾跨审计共享，第二份同名契约被误判。
    with tempfile.TemporaryDirectory() as tmp:
        store = AuditStore(Path(tmp) / "data")
        c_ok, _ = store.submit(parse_payload(
            isolation_contract("verify-iso-ok", INT)))
        check("存储层：兼容递归契约冻结为兼容", c_ok.compatible)

        c_bad, _ = store.submit(parse_payload(
            isolation_contract("verify-iso-bad", TEXT)))
        check(
            "存储层：同名不兼容契约在新审计标识下稳定拒绝",
            not c_bad.compatible
            and c_bad.mismatch is not None
            and c_bad.mismatch.code == ISO_MISMATCH_CODE
            and c_bad.mismatch.path == ISO_MISMATCH_PATH,
            getattr(c_bad.mismatch, "path", ""),
        )

        c_rev, _ = store.submit(parse_payload(
            isolation_contract("verify-iso-ok2", INT)))
        check("存储层：拒绝结论不反向污染同名兼容审计", c_rev.compatible)


# ---------------------------------------------------------------- 阶段 2/3

def phase_tests():
    print("[2/5] 执行代码测试（pytest）")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=ROOT,
    )
    check("pytest 全部通过", proc.returncode == 0, f"exit={proc.returncode}")


def phase_build():
    print("[3/5] 构建检查（字节码编译与模块导入）")
    proc = subprocess.run(
        [sys.executable, "-m", "compileall", "-q", "app", "scripts", "tests"],
        cwd=ROOT,
    )
    check("compileall 成功", proc.returncode == 0)
    proc = subprocess.run(
        [sys.executable, "-c",
         "import app.main, app.api, app.storage, app.subtype, app.parser"],
        cwd=ROOT,
    )
    check("关键模块均可导入", proc.returncode == 0)


# ---------------------------------------------------------------- 阶段 4

class _LocalServer:
    """进程内临时服务；restart() 在同一数据目录上换全新存储与服务，模拟容器重启。"""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name) / "data"
        self.server = None
        self.thread = None
        self.base_url = None

    def _start(self):
        store = AuditStore(self.data_dir)
        self.server = build_server("127.0.0.1", 0, store)
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.base_url

    def restart(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        return self._start()

    def __enter__(self):
        self._start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()


def http(method, url, body=None, timeout=5):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def wait_health(base_url, attempts=30):
    for _ in range(attempts):
        try:
            status, data = http("GET", f"{base_url}/health")
            if status == 200 and data.get("status") == "ok":
                return True
        except OSError:
            pass
        time.sleep(0.5)
    return False


def phase_api_smoke(base_url):
    print(f"[4/5] 审计接口冒烟（{base_url}）")
    check("健康检查 /health 可达", wait_health(base_url))

    stamp = str(int(time.time() * 1000))
    ok_id = f"SMOKE-OK-{stamp}"

    recursive = {
        "audit_id": ok_id,
        "root_name": "List",
        "sender_types": [{"name": "List", "type": variant(
            tag("Nil", rec()),
            tag("Cons", rec(field("head", INT),
                            field("tail", ref("List"), False))),
        )}],
        "receiver_types": [{"name": "List", "type": variant(
            tag("Nil", rec()),
            tag("Cons", rec(field("head", INT),
                            field("tail", ref("List"), False))),
        )}],
    }
    status, data = http("POST", f"{base_url}/api/audits", recursive)
    check("递归契约提交得 201 且兼容",
          status == 201 and data["conclusion"]["compatible"] is True,
          f"status={status}")

    status, data = http("POST", f"{base_url}/api/audits", recursive)
    check("相同契约重传得 200 且标记幂等",
          status == 200 and data.get("resubmitted_same_contract") is True,
          f"status={status}")
    frozen_at = data["conclusion"]["frozen_at"]

    changed = json.loads(json.dumps(recursive))
    changed["receiver_types"][0]["type"]["tags"][1]["type"]["fields"][0]["type"] = TEXT
    status, data = http("POST", f"{base_url}/api/audits", changed)
    check("改变契约得 409 且拒绝改写",
          status == 409 and data["error"] == "contract-conflict",
          f"status={status}")

    status, data = http("GET", f"{base_url}/api/audits/{ok_id}")
    check("重开仍读原冻结兼容结论",
          status == 200
          and data["conclusion"]["compatible"] is True
          and data["conclusion"]["frozen_at"] == frozen_at,
          f"status={status}")

    bad_id = f"SMOKE-MISSING-{stamp}"
    missing = {
        "audit_id": bad_id,
        "sender_types": [{"name": "R", "type": rec(field("a", INT))}],
        "receiver_types": [{"name": "R", "type": rec(
            field("a", INT), field("b", TEXT))}],
    }
    status, data = http("POST", f"{base_url}/api/audits", missing)
    check("字段缺失契约返回不兼容与稳定路径",
          status == 201
          and data["conclusion"]["compatible"] is False
          and data["conclusion"]["mismatch"]["path"] == "R.b",
          f"status={status}")

    tag_id = f"SMOKE-TAG-{stamp}"
    extra_tag = {
        "audit_id": tag_id,
        "sender_types": [{"name": "R", "type": variant(
            tag("A", INT), tag("B", INT))}],
        "receiver_types": [{"name": "R", "type": variant(tag("A", INT))}],
    }
    status, data = http("POST", f"{base_url}/api/audits", extra_tag)
    check("额外发送变体返回 extra-tag",
          status == 201
          and data["conclusion"]["mismatch"]["code"] == "extra-tag",
          f"status={status}")

    bad_body = {"audit_id": "bad id", "sender_types": [], "receiver_types": []}
    status, data = http("POST", f"{base_url}/api/audits", bad_body)
    check("非法契约一次返回多条问题",
          status == 400 and len(data.get("issues", [])) >= 2,
          f"issues={len(data.get('issues', []))}")


# ---------------------------------------------------------------- 阶段 5

def isolation_audit_ids(token):
    return {
        "ok": f"ISO-OK-{token}",
        "bad": f"ISO-BAD-{token}",
        "rev_bad": f"ISO-REV-BAD-{token}",
        "rev_ok": f"ISO-REV-OK-{token}",
    }


def check_bad_conclusion(concl, step):
    mm = concl.get("mismatch") or {}
    check(
        step,
        concl.get("compatible") is False
        and mm.get("code") == ISO_MISMATCH_CODE
        and mm.get("path") == ISO_MISMATCH_PATH,
        f"{mm.get('code')} @ {mm.get('path')}",
    )


def phase_audit_isolation(base_url, token):
    """同名递归契约的两份独立审计：递归裁决互不影响。"""
    print(f"[5/5] 审计隔离场景（真实 API，{base_url}）")
    ids = isolation_audit_ids(token)

    # 先提交兼容契约：根类型含必需载荷引用，Data 含整数序号与可选自引用后继。
    status, data = http("POST", f"{base_url}/api/audits",
                        isolation_contract(ids["ok"], INT))
    check("兼容递归契约提交被接受",
          status in (200, 201)
          and data["conclusion"]["compatible"] is True,
          f"status={status}")

    # 换用全新审计标识，提交同名同拓扑契约：接收端在 Data 序号处要求文本。
    status, data = http("POST", f"{base_url}/api/audits",
                        isolation_contract(ids["bad"], TEXT))
    check("第二份独立审计提交被受理", status in (200, 201), f"status={status}")
    check_bad_conclusion(
        data["conclusion"],
        "第二份审计稳定拒绝：Data 序号基本类型违约")
    frozen_bad = data["conclusion"]["frozen_at"]

    # 相同契约重传：幂等读取同一拒绝结论，冻结时间不变。
    status, data = http("POST", f"{base_url}/api/audits",
                        isolation_contract(ids["bad"], TEXT))
    check("相同契约重传保持拒绝结论与冻结时间",
          status == 200
          and data.get("resubmitted_same_contract") is True
          and data["conclusion"]["compatible"] is False
          and data["conclusion"]["frozen_at"] == frozen_bad,
          f"status={status}")

    # 改变契约重传：409，拒绝改写。
    status, data = http("POST", f"{base_url}/api/audits",
                        isolation_contract(ids["bad"], INT))
    check("改变契约重传被拒 409 且不改写",
          status == 409 and data.get("error") == "contract-conflict",
          f"status={status}")

    # 按标识重开：从冻结记录返回同一拒绝结论。
    status, data = http("GET", f"{base_url}/api/audits/{ids['bad']}")
    check("按标识重开返回同一拒绝结论",
          status == 200 and data["conclusion"]["frozen_at"] == frozen_bad,
          f"status={status}")
    if status == 200:
        check_bad_conclusion(data["conclusion"], "重开结论的违约路径与类别一致")

    # 相反提交顺序：先不兼容、再提交同名且兼容的独立审计，不得误拒。
    status, data = http("POST", f"{base_url}/api/audits",
                        isolation_contract(ids["rev_bad"], TEXT))
    check("反向顺序：不兼容契约先行冻结",
          status in (200, 201)
          and data["conclusion"]["compatible"] is False,
          f"status={status}")
    status, data = http("POST", f"{base_url}/api/audits",
                        isolation_contract(ids["rev_ok"], INT))
    check("反向顺序：同名兼容契约不被误拒",
          status in (200, 201)
          and data["conclusion"]["compatible"] is True,
          f"status={status}")

    # 第一份兼容审计保持原结论不变。
    status, data = http("GET", f"{base_url}/api/audits/{ids['ok']}")
    check("兼容审计结论不受后续审计影响",
          status == 200 and data["conclusion"]["compatible"] is True,
          f"status={status}")


def phase_reopen_after_restart(base_url, token):
    """服务重启后的读取：冻结结论按标识重开仍正确。"""
    print(f"[*] 服务重启后的冻结结论读取（{base_url}）")
    check("重启后健康检查 /health 可达", wait_health(base_url))
    ids = isolation_audit_ids(token)
    expectations = [
        (ids["ok"], True),
        (ids["bad"], False),
        (ids["rev_bad"], False),
        (ids["rev_ok"], True),
    ]
    for audit_id, compatible in expectations:
        status, data = http("GET", f"{base_url}/api/audits/{audit_id}")
        concl = data.get("conclusion", {})
        check(
            f"重启后重开 {audit_id} 结论正确",
            status == 200 and concl.get("compatible") is compatible,
            f"status={status}",
        )
        if status == 200 and not compatible:
            check_bad_conclusion(concl, f"重启后 {audit_id} 的违约路径与类别一致")


def main() -> int:
    base_url = os.environ.get("AUDIT_SMOKE_BASE_URL")
    token = os.environ.get("AUDIT_RUN_TOKEN") or str(int(time.time() * 1000))
    recheck_only = os.environ.get("AUDIT_PHASE") == "recheck"
    try:
        if recheck_only:
            # Compose 下由 acceptance.sh 在重启 web 后驱动：只读复核。
            if not base_url:
                raise Failure("AUDIT_PHASE=recheck 需要 AUDIT_SMOKE_BASE_URL")
            phase_reopen_after_restart(base_url.rstrip("/"), token)
        else:
            for phase in (phase_engine_recheck, phase_tests, phase_build):
                phase()
            if base_url:
                url = base_url.rstrip("/")
                phase_api_smoke(url)
                phase_audit_isolation(url, token)
                print("  [..] 服务重启后的读取：由 scripts/acceptance.sh "
                      "重启 web 后以 AUDIT_PHASE=recheck 复核")
            else:
                with _LocalServer() as server:
                    phase_api_smoke(server.base_url)
                    phase_audit_isolation(server.base_url, token)
                    phase_reopen_after_restart(server.restart(), token)
    except Failure as exc:
        print(f"\nVERIFY FAILED at: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # 冒烟基础设施错误也算验收失败
        print(f"\nVERIFY ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print("\nVERIFY OK: 递归兼容复核、测试、构建检查、接口冒烟、审计隔离全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
