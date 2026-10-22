"""Watch saved Marriott responses; no browser or hotel requests are made here."""

import argparse
import asyncio
import json
from pathlib import Path

from main import RunLock
from passive_config import load_passive_config
from passive_monitor import CaptureWatcher, PassiveMonitor
from passive_storage import PassiveStorage


async def watch(settings, *, once=False, report=False):
    with RunLock(settings.sqlite_path.with_suffix(".lock")):
        store = PassiveStorage(settings.sqlite_path)
        try:
            if report:
                result = store.report()
                print(json.dumps(result, ensure_ascii=False))
                return result
            watcher = CaptureWatcher(PassiveMonitor(settings, store))
            print(f"监控已启动：{settings.captures_dir}；通知渠道：{settings.notifications.channel}")
            while True:
                result = await watcher.tick()
                if result["files"] or any(result[k] for k in ("sent", "dry_run", "unknown")):
                    print("[监控结果] " + json.dumps(result, ensure_ascii=False))
                if once:
                    return store.report()
                await asyncio.sleep(settings.scan_interval_seconds)
        finally:
            store.close()


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(Path(__file__).resolve().parents[1] / "config.passive.yaml"))
    parser.add_argument("--once", action="store_true", help="导入现有文件一次，不等待或触发查价")
    parser.add_argument("--report", action="store_true", help="只显示历史及候选统计")
    args = parser.parse_args()
    try:
        asyncio.run(watch(load_passive_config(Path(args.config)), once=args.once, report=args.report))
    except KeyboardInterrupt:
        print("监控已停止，历史与候选保留。")
    except (ValueError, TypeError, RuntimeError, OSError):
        parser.exit(2, "监控无法启动，请检查配置、路径或是否已有进程运行；未输出凭据。\n")


if __name__ == "__main__":
    cli()
