"""
万豪被动采集器（只监听，不控制页面）

做法：
  1. 用一个正常启动的 Chrome（不经过 Playwright，不带自动化标志），仅额外开一个本机调试端口；
  2. 你像平时一样在浏览器里查价；
  3. 本脚本通过调试端口只订阅 Network 事件，遇到万豪"房价接口"的响应就脱敏后存成 JSON。
  不导航、不点击、不注入脚本、不发任何额外请求。

用法（先 pip install websockets）：
    python collector/passive_capture.py --launch
    # 在弹出的 Chrome 里正常搜索酒店、选日期、看房价；Ctrl+C 结束

    # 如果 Chrome 已由你手动带参数启动：
    #   Windows: chrome.exe --remote-debugging-port=9222 --user-data-dir=C:\\hotel-profile
    #   macOS:   "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \\
    #              --remote-debugging-port=9222 --user-data-dir=$HOME/hotel-profile
    python collector/passive_capture.py

安全说明：
  - 不保存任何请求头 / cookie / 令牌；请求变量里的 customerId 等敏感键替换为 REDACTED；
  - 写盘前再扫描一遍 JWT 和邮箱，发现就拒绝写入；
  - 调试端口只监听本机 127.0.0.1。不要在端口开着时访问不信任的网页，用完关掉这个 Chrome。
  - 新版 Chrome 不允许对默认 profile 开调试端口，所以必须用独立的 --user-data-dir。
    第一次用时这个 profile 是空的；建议先不登录，用公开价；需要会员价再自行登录。
"""

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

TARGET_PATHS = {"/mi/query/PhoenixBookDTTSearchProductsByProperty"}
TARGET_HOST_SUFFIXES = ("marriott.com.cn", "marriott.com")
TARGET_HOSTS = {"www.marriott.com.cn", "www.marriott.com"}
OPERATION = "PhoenixBookDTTSearchProductsByProperty"
BASE = Path(__file__).resolve().parent
DEFAULT_OUT = BASE.parent / "captures" / "marriott"
DEFAULT_PROFILE = BASE / ".profiles" / "marriott-passive"

SENSITIVE_KEY = re.compile(
    r"customerid|token|authorization|cookie|e-?mail|phone|rewardsnumber|membernumber|password|guestid|"
    r"session|secret|firstname|lastname|creditcard|cardnumber|cvv|securitycode|billing|headers",
    re.I,
)
JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def capture_event(value):
    """Local event identity, not authentication: changing a timestamp cannot create a second event."""
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "event_id", "collector_id"}
        or type(value.get("version")) is not int
        or value["version"] != 1
        or not isinstance(value.get("event_id"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", value["event_id"])
        or not isinstance(value.get("collector_id"), str)
        or not re.fullmatch(r"[0-9a-f]{32}", value["collector_id"])
    ):
        raise ValueError("被动采集事件标识无效")
    return dict(value)


# ---------------------------------------------------------------- 纯逻辑（可离线测试）
def is_target(url: str, method: str = "POST") -> bool:
    if method.upper() != "POST":
        return False
    try:
        u = urlparse(url)
        return (
            u.scheme == "https"
            and u.hostname in TARGET_HOSTS
            and not u.username
            and not u.password
            and u.port in (None, 443)
            and u.path in TARGET_PATHS
        )
    except (TypeError, ValueError):
        return False


def redact(obj):
    """递归替换敏感键的值。"""
    if isinstance(obj, dict):
        return {k: ("REDACTED" if SENSITIVE_KEY.search(k) else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    return obj


def process_capture(url: str, post_text: str, body_text: str) -> dict:
    """把一次请求/响应变成可入库的脱敏记录。有任何问题就抛 ValueError，由调用方记录并跳过。"""
    if not is_target(url):
        raise ValueError("不是已验证的万豪官网房价来源")
    post = json.loads(post_text)
    resp = json.loads(body_text)
    if not isinstance(post, dict) or post.get("operationName") != OPERATION:
        raise ValueError("不是已验证的房价操作")
    if not isinstance(resp, dict) or resp.get("errors") or not resp.get("data"):
        raise ValueError("响应没有 data（可能是错误响应）")

    if not isinstance(post.get("variables"), dict):
        raise ValueError("请求变量结构无效")
    variables = redact(post["variables"])
    search = variables.get("search", {}) or {}
    if not isinstance(search, dict):
        raise ValueError("酒店查询结构无效")
    options = search.get("options", {}) or {}
    if not isinstance(options, dict):
        raise ValueError("日期查询结构无效")
    identity = search.get("propertyId")
    if not isinstance(identity, str) or not re.fullmatch(r"[A-Z0-9]{5}", identity):
        raise ValueError("缺少有效酒店代码")
    ci, co = date.fromisoformat(options.get("startDate", "")), date.fromisoformat(options.get("endDate", ""))
    if co <= ci:
        raise ValueError("入住日期必须早于离店日期")
    for key in ("offset", "limit"):
        if key in variables and (
            type(variables[key]) is not int or variables[key] < (1 if key == "limit" else 0)
        ):
            raise ValueError("分页字段无效")

    record = {
        "source_site": urlparse(url).hostname,
        "operation": post.get("operationName"),
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "property_id": search.get("propertyId"),
        "check_in": options.get("startDate"),
        "check_out": options.get("endDate"),
        "variables": variables,
        "response": redact(resp),
    }
    text = json.dumps(record, ensure_ascii=False)
    if JWT_RE.search(text) or EMAIL_RE.search(text):
        raise ValueError("脱敏后仍检测到疑似令牌/邮箱，拒绝写入")
    return record


def content_hash(record: dict) -> str:
    core = {
        "site": record["source_site"],
        "operation": record["operation"],
        "v": record["variables"],
        "r": record["response"],
    }
    return hashlib.sha256(json.dumps(core, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def save_record(record: dict, out_dir: Path, seen: set) -> Path | None:
    original_time = record.get("captured_at")
    event = capture_event(record["capture_event"]) if "capture_event" in record else None
    # Revalidate before writing, even when called without process_capture.
    record = process_capture(
        "https://" + record["source_site"] + next(iter(TARGET_PATHS)),
        json.dumps({"operationName": record["operation"], "variables": record["variables"]}),
        json.dumps(record["response"]),
    )
    if original_time is not None:
        parsed_time = datetime.fromisoformat(original_time.replace("Z", "+00:00"))
        if parsed_time.tzinfo is None:
            raise ValueError("采集时间缺少时区")
        record["captured_at"] = parsed_time.astimezone(timezone.utc).isoformat()
    h = content_hash(record)
    # Identical prices from distinct browser requests are valid confirmation
    # evidence. Only repeated delivery of the SAME event is a duplicate.
    identity = event["event_id"] if event else h
    if identity in seen:
        return None
    if event:
        record["capture_event"] = event
    out_dir.mkdir(parents=True, exist_ok=True)
    offset = (record["variables"] or {}).get("offset", 0)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = (
        f"{record['source_site']}_{record['property_id']}_{record['check_in']}_"
        f"{record['check_out']}_off{offset}_{ts}_{h}{'_' + identity[:12] if event else ''}.json"
    )
    path = out_dir / name
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)  # Watchers never read an incompletely written JSON.
    seen.add(identity)
    return path


# ---------------------------------------------------------------- Chrome 启动
def find_chrome() -> str | None:
    if sys.platform.startswith("win"):
        for base in (
            os.environ.get("PROGRAMFILES"),
            os.environ.get("PROGRAMFILES(X86)"),
            os.environ.get("LOCALAPPDATA"),
        ):
            if base:
                p = Path(base) / "Google/Chrome/Application/chrome.exe"
                if p.exists():
                    return str(p)
    elif sys.platform == "darwin":
        p = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        if p.exists():
            return str(p)
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        p = shutil.which(name)
        if p:
            return p
    return None


def debugger_info(port: int):
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("调试端口无效")
    # Never send the local debugging endpoint through an environment proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    expected = f"http://127.0.0.1:{port}/json/version"
    with opener.open(expected, timeout=2) as response:
        if response.url != expected:
            raise ValueError("调试地址发生意外跳转")
        info = json.load(response)
    ws = urlparse(info.get("webSocketDebuggerUrl", ""))
    if (
        ws.scheme != "ws"
        or ws.hostname not in ("127.0.0.1", "localhost", "::1")
        or ws.port != port
        or ws.username
        or ws.password
        or not ws.path.startswith("/devtools/browser/")
    ):
        raise ValueError("调试 WebSocket 必须位于指定的本机端口")
    return info


def ensure_chrome(args):
    if not 1 <= args.port <= 65535:
        sys.exit("调试端口必须在 1–65535 之间。")
    try:
        return debugger_info(args.port)
    except Exception:
        pass
    if not args.launch:
        sys.exit(
            f"连不上 127.0.0.1:{args.port} 的 Chrome 调试端口。加 --launch 让脚本帮你启动，或按文件头说明手动启动。"
        )
    chrome = args.chrome or find_chrome()
    if not chrome:
        sys.exit("没找到 Chrome，请用 --chrome 指定路径。")
    profile = Path(args.profile).expanduser().resolve()
    profile.mkdir(parents=True, exist_ok=True)
    cmd = [
        chrome,
        f"--remote-debugging-port={args.port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        args.start_url,
    ]
    args._launched_process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        time.sleep(0.5)
        try:
            return debugger_info(args.port)
        except Exception:
            continue
    sys.exit("Chrome 已启动但调试端口没有响应。")


# ---------------------------------------------------------------- CDP 监听（仅 Network）
class Collector:
    def __init__(self, ws, out_dir: Path):
        self.ws = ws
        self.out_dir = out_dir
        self._id = 0
        self._futs = {}
        self.pending = {}  # (sessionId, requestId) -> dict
        self.seen = set()
        self.count = 0
        self.tasks = set()
        self.sessions = set()
        self.collector_id = uuid4().hex

    async def send(self, method, params=None, session=None):
        self._id += 1
        msg = {"id": self._id, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        fut = asyncio.get_running_loop().create_future()
        self._futs[self._id] = fut
        try:
            await self.ws.send(json.dumps(msg))
            return await asyncio.wait_for(fut, 20)
        finally:
            self._futs.pop(msg["id"], None)

    async def reader(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if "id" in msg:
                    fut = self._futs.pop(msg["id"], None)
                    if fut and not fut.done():
                        if "error" in msg:
                            fut.set_exception(RuntimeError("CDP command failed"))
                        else:
                            fut.set_result(msg.get("result", {}))
                else:
                    task = asyncio.create_task(self.on_event(msg))
                    self.tasks.add(task)
                    task.add_done_callback(self.tasks.discard)
        finally:
            for future in list(self._futs.values()):
                if not future.done():
                    future.set_exception(ConnectionError("Local Chrome connection closed"))

    async def attach_network(self, session):
        if session in self.sessions:
            return
        self.sessions.add(session)
        await self.send(
            "Network.enable",
            {"maxTotalBufferSize": 50_000_000, "maxResourceBufferSize": 10_000_000},
            session,
        )

    async def on_event(self, msg):
        try:
            method, p, sid = msg.get("method"), msg.get("params", {}), msg.get("sessionId")
            if method == "Target.attachedToTarget":
                if p["targetInfo"].get("type") == "page":
                    await self.attach_network(p["sessionId"])
            elif method == "Network.requestWillBeSent":
                req = p["request"]
                key = (sid, p["requestId"])
                if is_target(req["url"], req.get("method", "")):
                    self.pending[key] = {
                        "url": req["url"],
                        "post": req.get("postData"),
                        "status": None,
                    }
                else:
                    # Redirects can reuse the same CDP request ID. Never label
                    # another origin's body as an official Marriott response.
                    self.pending.pop(key, None)
            elif method == "Network.responseReceived":
                key = (sid, p["requestId"])
                if key in self.pending:
                    response = p["response"]
                    if not is_target(response.get("url", self.pending[key]["url"])):
                        self.pending.pop(key, None)
                    else:
                        self.pending[key]["status"] = response["status"]
            elif method == "Network.loadingFinished":
                key = (sid, p["requestId"])
                item = self.pending.pop(key, None)
                if item:
                    await self.handle_finished(sid, p["requestId"], item)
            elif method == "Network.loadingFailed":
                self.pending.pop((sid, p["requestId"]), None)
            elif method == "Target.detachedFromTarget":
                detached = p.get("sessionId")
                self.sessions.discard(detached)
                self.pending = {key: item for key, item in self.pending.items() if key[0] != detached}
        except Exception as e:  # Do not put browser-supplied error messages in logs.
            print(f"[警告] 处理事件出错: {type(e).__name__}", file=sys.stderr)

    async def handle_finished(self, sid, request_id, item):
        if item["status"] != 200:
            print(f"[跳过] 状态码 {item['status']}（不重试，不处理）")
            return
        post = item["post"]
        if post is None:
            post = (await self.send("Network.getRequestPostData", {"requestId": request_id}, sid)).get(
                "postData"
            )
        body = await self.send("Network.getResponseBody", {"requestId": request_id}, sid)
        text = body["body"]
        if body.get("base64Encoded"):
            import base64

            text = base64.b64decode(text).decode("utf-8", "replace")
        try:
            record = process_capture(item["url"], post, text)
        except ValueError:
            print("[跳过] 脱敏或格式校验未通过（不输出原始内容）")
            return
        record["capture_event"] = {
            "version": 1,
            "collector_id": self.collector_id,
            "event_id": hashlib.sha256(f"{self.collector_id}|{sid}|{request_id}".encode()).hexdigest(),
        }
        path = save_record(record, self.out_dir, self.seen)
        if path:
            self.count += 1
            print(
                f"[已保存 #{self.count}] {record['property_id']} {record['check_in']}→{record['check_out']}  {path.name}"
            )

    async def close(self):
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*list(self.tasks), return_exceptions=True)
        self.pending.clear()
        for future in self._futs.values():
            if not future.done():
                future.cancel()
        self._futs.clear()


async def _run(args):
    import websockets  # pip install websockets

    monitor_task = None
    monitor_config = getattr(args, "monitor_config", None)
    if monitor_config:
        from passive_config import load_passive_config

        settings = load_passive_config(Path(monitor_config))
        if settings.captures_dir != Path(args.out).resolve():
            raise ValueError("采集目录与监控配置 captures_dir 必须一致")
    info = ensure_chrome(args)
    async with websockets.connect(info["webSocketDebuggerUrl"], max_size=25_000_000, proxy=None) as ws:
        c = Collector(ws, Path(args.out))
        reader = asyncio.create_task(c.reader())
        try:
            if monitor_config:
                from scripts.monitor_passive import watch

                monitor_task = asyncio.create_task(watch(settings))
            await c.send(
                "Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True}
            )
            print("监听中：请在 Chrome 里正常查价。Ctrl+C 结束。")
            if monitor_task:
                finished, _ = await asyncio.wait({reader, monitor_task}, return_when=asyncio.FIRST_COMPLETED)
                for task in finished:
                    await task
            else:
                await reader
        finally:
            await c.close()
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
            if monitor_task:
                monitor_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await monitor_task


async def run(args):
    from main import RunLock

    # Two listeners must not label one browser request as two independent events.
    with RunLock(Path(args.out).resolve() / ".capture.lock"):
        await _run(args)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--launch", action="store_true", help="没有可连的 Chrome 时由脚本启动")
    ap.add_argument("--chrome", help="Chrome 可执行文件路径（默认自动查找）")
    ap.add_argument("--profile", default=str(DEFAULT_PROFILE), help="独立 profile 目录")
    ap.add_argument("--start-url", default="https://www.marriott.com.cn/")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--monitor-config", help="同时启动本地历史、降价检测和通知（YAML配置路径）")
    args = ap.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("已结束。")
    except Exception as error:
        ap.exit(
            2,
            f"监听已停止（{type(error).__name__}）。请检查配置、调试端口和是否重复运行；未输出原始错误内容。\n",
        )


if __name__ == "__main__":
    main()
