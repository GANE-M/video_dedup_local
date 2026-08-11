from __future__ import annotations

import argparse
import json
import sys

import uvicorn

from .database import GatewayDatabase
from .settings import GatewaySettings
from .storage import JobStorage


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="video-dedup-local web gateway")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="启动本地网页与API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    key = sub.add_parser("create-key", help="创建外部用户访问密钥")
    key.add_argument("--label", required=True)
    key.add_argument("--maximum-active-jobs", type=int, default=3)
    key.add_argument("--expires-at", default=None, help="ISO时间；留空表示不自动到期")
    sub.add_parser("list-keys", help="列出访问密钥记录（不显示密钥正文）")
    disable = sub.add_parser("disable-key", help="立即禁用一个访问密钥")
    disable.add_argument("key_id")
    report = sub.add_parser("storage-report", help="查看服务器端全部账号或指定账号的存储占用")
    report.add_argument("--key-id", default="", help="留空表示全部账号")
    cleanup = sub.add_parser("storage-cleanup", help="清理服务器端账号资源（默认只预估）")
    cleanup.add_argument("--key-id", required=True)
    cleanup.add_argument(
        "--category",
        action="append",
        choices=("chunks", "inputs", "completed_runtime", "failed_cancelled"),
        default=[],
    )
    cleanup.add_argument("--older-than-days", type=int, default=7)
    cleanup.add_argument("--execute", action="store_true", help="真正删除；不传时只预估")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    settings = GatewaySettings.from_environment()
    settings.ensure_directories()
    if args.command == "serve":
        uvicorn.run("web_gateway.app:create_app", factory=True, host=args.host, port=args.port, reload=False)
        return 0
    database = GatewayDatabase(settings.database_path)
    storage = JobStorage(settings)
    if args.command == "create-key":
        secret, record = database.create_access_key(
            args.label, maximum_active_jobs=args.maximum_active_jobs, expires_at=args.expires_at
        )
        print(json.dumps({"access_key": secret, "record": record}, ensure_ascii=False, indent=2))
        print("注意：访问密钥只在这里完整显示一次。", file=sys.stderr)
    elif args.command == "list-keys":
        print(json.dumps(database.list_access_keys(), ensure_ascii=False, indent=2))
    elif args.command == "disable-key":
        try:
            database.disable_access_key(args.key_id)
        except KeyError:
            print(f"密钥记录不存在: {args.key_id}", file=sys.stderr)
            return 1
        print(json.dumps({"event": "ACCESS_KEY_DISABLED", "key_id": args.key_id}, ensure_ascii=False))
    elif args.command == "storage-report":
        jobs = database.list_all_jobs()
        key_ids = [args.key_id] if args.key_id else sorted({str(job["access_key_id"]) for job in jobs})
        payload = {
            key_id: storage.account_usage(jobs, key_id)
            for key_id in key_ids
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif args.command == "storage-cleanup":
        jobs = database.list_all_jobs()
        payload = storage.cleanup_account_jobs(
            jobs,
            args.key_id,
            categories=args.category or ["chunks"],
            older_than_days=args.older_than_days,
            dry_run=not args.execute,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
