"""服务入口：python -m app.main，端口由环境变量 PORT 配置（容器/宿主映射）。"""
from __future__ import annotations

import os

from .api import build_server
from .storage import AuditStore


def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    data_dir = os.environ.get("AUDIT_DATA_DIR", "/data")
    store = AuditStore(data_dir)
    server = build_server(host, port, store)
    print(f"payload-audit listening on {host}:{port} (data={data_dir})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
