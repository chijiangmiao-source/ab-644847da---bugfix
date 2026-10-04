#!/usr/bin/env python3
"""真实审计 API 自动化验收客户端（仅用标准库）。

由 scripts/acceptance.sh 在 Compose 启动服务并确认 /health 后调用：

  live <base_url> --state <file>
      连续提交两份同名递归拓扑的独立审计（先兼容、后不兼容），
      核对第二份稳定拒绝及其首个违约（primitive-mismatch @
      Cmd.payload[Data].seq）；再覆盖相反提交顺序、相同契约重传、
      按标识重开与不同契约 409 冲突。标识/指纹/frozen_at 写入状态文件。

  legacy <base_url>
      对 scripts/seed_legacy.py 预置的旧版本（跨审计缓存污染期）冻结记录，
      按标识重开与相同契约重传，核对按冻结原始契约恢复的正确结论，
      且 audit_id / 契约指纹 / frozen_at 逐字保留；不同契约重传仍 409。

  persist <base_url> --state <file>
      服务重启后读取 live 阶段的全部审计与已修复的旧记录，
      核对结论、指纹、冻结时间在重启后保持稳定。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.request
from urllib.error import HTTPError

INT = {"kind": "int"}
TEXT = {"kind": "text"}

# seed_legacy.py 预置的旧记录标识。
LEGACY_BAD = "LEGACY-SEED-BAD"
LEGACY_GOOD = "LEGACY-SEED-GOOD"
LEGACY_BAD_FROZEN_AT = "2026-01-01T00:00:00Z"
LEGACY_GOOD_FROZEN_AT = "2026-02-02T00:00:00Z"


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


def payload_contract(audit_id, seq_type):
    """根类型 Cmd 含必需载荷引用；载荷是带 Data 标签的递归变体；
    Data 含整数序号 seq 与可选自引用后继 next。"""
    sender = [
        {"name": "Cmd", "type": rec(field("payload", ref("Payload")))},
        {"name": "Payload", "type": variant(tag("Data", ref("Data")))},
        {"name": "Data", "type": rec(
            field("seq", INT), field("next", ref("Data"), required=False))},
    ]
    receiver = [
        {"name": "Cmd", "type": rec(field("payload", ref("Payload")))},
        {"name": "Payload", "type": variant(tag("Data", ref("Data")))},
        {"name": "Data", "type": rec(
            field("seq", seq_type), field("next", ref("Data"), required=False))},
    ]
    return {
        "audit_id": audit_id,
        "root_name": "Cmd",
        "sender_types": sender,
        "receiver_types": receiver,
    }


def canonical_fingerprint(body):
    """与服务端 storage.canonical_fingerprint 相同的契约指纹算法。"""
    fingerprint_dict = {
        "audit_id": body["audit_id"],
        "root_name": body.get("root_name"),
        "sender": body["sender_types"],
        "receiver": body["receiver_types"],
    }
    blob = json.dumps(
        fingerprint_dict, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


class Failure(Exception):
    pass


def check(step, condition, detail=""):
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {step}{(' — ' + detail) if detail else ''}")
    if not condition:
        raise Failure(step)


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


def wait_health(base_url, attempts=60):
    for _ in range(attempts):
        try:
            status, data = http("GET", f"{base_url}/health")
            if status == 200 and data.get("status") == "ok":
                return True
        except OSError:
            pass
        time.sleep(1)
    return False


def submit(base_url, body):
    return http("POST", f"{base_url}/api/audits", body)


def reopen(base_url, audit_id):
    return http("GET", f"{base_url}/api/audits/{audit_id}")


def assert_rejected_primitive(c, where):
    check(f"{where}：稳定拒绝", c["compatible"] is False)
    mm = c.get("mismatch")
    check(f"{where}：首个违约为基本类型违约", bool(mm) and mm["code"] == "primitive-mismatch",
          str(mm and mm.get("code")))
    check(
        f"{where}：违约定位到载荷 Data 分支序号",
        bool(mm) and mm["path"] == "Cmd.payload[Data].seq",
        str(mm and mm.get("path")),
    )


def snapshot(c):
    return {
        "compatible": c["compatible"],
        "frozen_at": c["frozen_at"],
        "contract_fingerprint": c["contract_fingerprint"],
    }


def phase_live(base_url, state_path):
    print("[acceptance] 阶段 live：连续独立审计 / 相反顺序 / 重传 / 重开 / 冲突")
    check("健康检查 /health 可达", wait_health(base_url))

    stamp = f"{int(time.time() * 1000)}-{int(time.time() * 1000000) % 1000000:06d}"
    ok_id = f"ACC-OK-{stamp}"
    bad_id = f"ACC-BAD-{stamp}"
    rev_bad_id = f"ACC-REV-BAD-{stamp}"
    rev_ok_id = f"ACC-REV-OK-{stamp}"
    state = {"ids": {ok_id: None, bad_id: None, rev_bad_id: None, rev_ok_id: None}}

    # 1) 先兼容，再以全新标识提交同名拓扑但接收端要求 text 的契约。
    good_body = payload_contract(ok_id, INT)
    status, data = submit(base_url, good_body)
    check("第一份兼容契约 201 且 compatible=true",
          status == 201 and data["conclusion"]["compatible"] is True, f"status={status}")
    state["ids"][ok_id] = snapshot(data["conclusion"])
    check("第一份指纹与契约一致",
          data["conclusion"]["contract_fingerprint"] == canonical_fingerprint(good_body))

    bad_body = payload_contract(bad_id, TEXT)
    status, data = submit(base_url, bad_body)
    check("第二份不兼容契约 201（独立新建）", status == 201, f"status={status}")
    assert_rejected_primitive(data["conclusion"], "第二份审计")
    state["ids"][bad_id] = snapshot(data["conclusion"])
    check("第二份指纹与契约一致",
          data["conclusion"]["contract_fingerprint"] == canonical_fingerprint(bad_body))

    # 2) 第一份兼容审计结论保持不变。
    status, data = reopen(base_url, ok_id)
    check("重开第一份仍兼容且指纹/冻结时间不变",
          status == 200
          and snapshot(data["conclusion"]) == state["ids"][ok_id],
          f"status={status}")

    # 3) 相同契约重传：200 + 幂等标记，frozen_at 不变。
    status, data = submit(base_url, bad_body)
    check("第二份相同契约重传 200 且标记幂等",
          status == 200 and data.get("resubmitted_same_contract") is True,
          f"status={status}")
    assert_rejected_primitive(data["conclusion"], "第二份重传")
    check("重传结论指纹/冻结时间与冻结记录一致",
          snapshot(data["conclusion"]) == state["ids"][bad_id])

    # 4) 相反提交顺序：先不兼容，再提交同名拓扑的独立兼容审计。
    status, data = submit(base_url, payload_contract(rev_bad_id, TEXT))
    check("相反顺序：先提交的契约被拒",
          status == 201 and data["conclusion"]["compatible"] is False, f"status={status}")
    assert_rejected_primitive(data["conclusion"], "相反顺序-不兼容")
    state["ids"][rev_bad_id] = snapshot(data["conclusion"])

    status, data = submit(base_url, payload_contract(rev_ok_id, INT))
    check("相反顺序：后提交的独立兼容审计不被反向误拒",
          status == 201 and data["conclusion"]["compatible"] is True, f"status={status}")
    state["ids"][rev_ok_id] = snapshot(data["conclusion"])

    # 5) 不同契约重传仍须 409，且不改写冻结记录。
    status, data = submit(base_url, payload_contract(ok_id, TEXT))
    check("不同契约重传得 409 contract-conflict",
          status == 409 and data.get("error") == "contract-conflict", f"status={status}")
    status, data = reopen(base_url, ok_id)
    check("409 后原兼容冻结结论未被改写",
          status == 200 and snapshot(data["conclusion"]) == state["ids"][ok_id])

    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
    print(f"[acceptance] live 阶段状态已写入 {state_path}")


def phase_legacy(base_url):
    print("[acceptance] 阶段 legacy：旧版本误判冻结记录按原始契约恢复")
    check("健康检查 /health 可达", wait_health(base_url))

    bad_body = payload_contract(LEGACY_BAD, TEXT)
    expected_fp = canonical_fingerprint(bad_body)

    # 按标识重开：错误“兼容”结论须恢复为稳定拒绝。
    status, data = reopen(base_url, LEGACY_BAD)
    check("旧误兼容记录重开 200", status == 200, f"status={status}")
    c = data["conclusion"]
    assert_rejected_primitive(c, "旧误兼容记录")
    check("审计标识保留", c["audit_id"] == LEGACY_BAD)
    check("契约指纹保留并与契约一致", c["contract_fingerprint"] == expected_fp)
    check("冻结时间保留", c["frozen_at"] == LEGACY_BAD_FROZEN_AT, c["frozen_at"])
    healed_bad = snapshot(c)

    # 完全相同契约重传：同样恢复，200，frozen_at 不变。
    status, data = submit(base_url, bad_body)
    check("旧记录相同契约重传 200 幂等",
          status == 200 and data.get("resubmitted_same_contract") is True, f"status={status}")
    assert_rejected_primitive(data["conclusion"], "旧记录重传")
    check("重传保留原指纹/冻结时间", snapshot(data["conclusion"]) == healed_bad)

    # 不同契约重传：409 且记录不得改写。
    status, data = submit(base_url, payload_contract(LEGACY_BAD, INT))
    check("旧记录不同契约重传仍 409",
          status == 409 and data.get("error") == "contract-conflict", f"status={status}")
    status, data = reopen(base_url, LEGACY_BAD)
    check("409 后已修复结论不被改写",
          status == 200 and snapshot(data["conclusion"]) == healed_bad)

    # 反向污染：旧误拒绝记录重开后恢复兼容。
    good_body = payload_contract(LEGACY_GOOD, INT)
    status, data = reopen(base_url, LEGACY_GOOD)
    check("旧误拒绝记录重开 200", status == 200, f"status={status}")
    c = data["conclusion"]
    check("旧误拒绝记录恢复兼容", c["compatible"] is True)
    check("恢复后无首个违约", c.get("mismatch") is None)
    check("审计标识保留", c["audit_id"] == LEGACY_GOOD)
    check("契约指纹保留并与契约一致",
          c["contract_fingerprint"] == canonical_fingerprint(good_body))
    check("冻结时间保留", c["frozen_at"] == LEGACY_GOOD_FROZEN_AT, c["frozen_at"])


def phase_persist(base_url, state_path):
    print("[acceptance] 阶段 persist：服务重启后的读取")
    check("健康检查 /health 可达", wait_health(base_url))
    with open(state_path, "r", encoding="utf-8") as fh:
        state = json.load(fh)

    for audit_id, expected in state["ids"].items():
        status, data = reopen(base_url, audit_id)
        check(f"重启后重开 {audit_id} 结论/指纹/冻结时间稳定",
              status == 200 and snapshot(data["conclusion"]) == expected,
              f"status={status}")

    # 已修复的旧记录在重启后同样保持正确结论与原始冻结时间。
    status, data = reopen(base_url, LEGACY_BAD)
    check("重启后旧误兼容记录仍稳定拒绝",
          status == 200
          and data["conclusion"]["compatible"] is False
          and data["conclusion"]["mismatch"]["code"] == "primitive-mismatch"
          and data["conclusion"]["mismatch"]["path"] == "Cmd.payload[Data].seq"
          and data["conclusion"]["frozen_at"] == LEGACY_BAD_FROZEN_AT,
          f"status={status}")

    status, data = reopen(base_url, LEGACY_GOOD)
    check("重启后旧误拒绝记录仍保持兼容",
          status == 200
          and data["conclusion"]["compatible"] is True
          and data["conclusion"]["frozen_at"] == LEGACY_GOOD_FROZEN_AT,
          f"status={status}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="phase", required=True)
    for name in ("live", "legacy", "persist"):
        p = sub.add_parser(name)
        p.add_argument("base_url")
        p.add_argument("--state")
    args = parser.parse_args(argv)

    try:
        if args.phase == "live":
            phase_live(args.base_url.rstrip("/"), args.state)
        elif args.phase == "legacy":
            phase_legacy(args.base_url.rstrip("/"))
        else:
            phase_persist(args.base_url.rstrip("/"), args.state)
    except Failure as exc:
        print(f"\nACCEPTANCE FAILED at: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"\nACCEPTANCE ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(f"[acceptance] 阶段 {args.phase} 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
