# Hotelbug Collector

现已增加[全球六集团自动入口](../docs/GLOBAL_LOCAL_MONITOR.md)：官网目录续跑、
跨集团公平轮询、未来365天日期游标和独立暂停。启动：

```powershell
.\work\collector-venv\Scripts\python.exe -X utf8 -u .\collector\global_monitor.py
```

该命令在项目根目录运行，配置位于 `backend/local_global.toml`。
下面的单品牌工具保留用于采样与问题定位。

独立 Python 3.11 + Playwright + SQLite 版本；旧 NAS 项目和数据不受影响。
实施顺序见 [PLAN.md](PLAN.md)。希尔顿已完成真实样本解析和普通网页适配，默认仍为 mock；官网自动实采尚未验收。
万豪中国站已新增[自动查价](docs/AUTOMATIC_MARRIOTT.md)：复用本机 Chrome 独立 profile，
按配置清单逐酒店、逐日期操作官网，监听返回后保存并入库。2026-10-21 已实测连续两个入住日成功。
早先国际站的 403 暂停标记仍保留；中国站是独立的队列和页面流程。
轻量 `adapters/` 中其余四集团仍为采样接口；全球入口复用 `backend/` 已有的六集团官方页面采集器。
万豪 `fetch_prices` 用法和字段口径见 [万豪样本适配器](docs/MARRIOTT_SAMPLE_ADAPTER.md)。
新增用户手动操作 Chrome 的[被动采集器](docs/PASSIVE_CAPTURE.md)，支持中国站脱敏文件和所选方案政策补充。
现已接入[被动监控闭环](docs/PASSIVE_MONITOR.md)：新采集事件自动入库、历史35%跌幅、独立二次确认、
严格24小时去重及Bark/Telegram通知；默认console演练，不等于六集团自动查价已完成。

## 当前可用：万豪中国站自动查询

在项目根目录运行（本机现有环境）：

```powershell
.\work\collector-venv\Scripts\python.exe -X utf8 -u .\collector\automatic_monitor.py
```

它会自动启动或复用监听和检测进程，不需要另开被动采集命令。Chrome 会自行选日期、查看房价。
`collector/config.automatic.yaml` 配置酒店和滚动 365 天；已查日期默认 24 小时后复查，
发现低价候选则优先安排独立复核。官网售罄不写入零价格。中断后重新运行会续跑。

只查询一项加 `--once`，只看进度加 `--status`。通知渠道仍在 `config.passive.yaml`，
默认 `console` 不会推送手机。当前自动清单只有 XIYBP，未宣称已查遍全球或全年。

## 安装

在本目录运行：

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m playwright install chromium
.venv\Scripts\python -m pytest -q
```

Linux 对应使用 `python3.11` 和 `.venv/bin/python`；有桌面的电脑适合交互采样，NAS 暂不部署此新模块。

## 采样

```powershell
.venv\Scripts\python -m scripts.capture_har
```

需要现有路由器代理时：

```powershell
.venv\Scripts\python -m scripts.capture_har --proxy http://192.168.50.4:7890
```

若本机自带的 Chromium 无法启动，可使用 `--channel chrome`，仍是独立采样窗口，不使用你现有 Chrome 的登录资料。

在新窗口正常选择希尔顿酒店和一晚日期，查看现金报价；再修改日期或人数做对照。**关闭这张采样窗口即可结束并保存。**
也可用 `--seconds 180` 定时结束。输出在 `samples/<时间戳>/`，包括完整 `capture.har`、所有 JSON 响应原始文件、索引和会话结果。

403 / 429 / 可见验证码会停止，不绕过、不自动换代理或重试。检查原因后可显式用 `--resume-brand` 恢复下一次采样。
采样工具不接管现有 Chrome，不读取现有登录资料，若必须登录，应在新窗口自行操作。
完整 HAR 可能包含 Cookie / 会员信息，样本被 Git 忽略，不会自动上传；分享前请检查敏感信息。

HAR 关闭与保存依据 [Playwright 官方说明](https://playwright.dev/python/docs/api/class-browser#browser-new-context-option-record-har-path)。
采样中的 JSON 只是待分析证据，不是已验证的价格字段，第三方统计响应不会被当成酒店数据源。

## SQLite / 检测 / Bark（先用 mock 验证）

```powershell
.venv\Scripts\python -m main --once
.venv\Scripts\python -m main
```

第一条只轮询一次；第二条持续运行。`config.yaml` 设置关注清单、未来日期范围、请求延迟、阈值和复核等待。
默认只生成 3 个入住日的**模拟报价**；把 `dates.days` 改为 `365` 可枚举未来一年，不能据此声称已经获得一年实价。
SQLite 文件在 `data/monitor.sqlite`，历史、候选、品牌暂停和通知记录均可跨重启保留。日期按 UTC 当日生成。

检测条件：同酒店、日期、房型、房价类型、币种、税费和人数口径，当前价 ≤ 历史中位数 × 65%，至少 3 条历史观测。
达到条件后只写候选，等至少 60 秒再独立取价确认；不把第一次低价直接推送。过期或不同条件的结果会被拒绝。
`--once` 不阻塞等待复核，下次运行会继续未完成的候选。普通轮询间隔与复核检查分开，等待复核不会额外高频重查全部酒店。

Bark 默认关闭；未来实价接通后，在配置启用并设置环境变量：

```powershell
$env:BARK_DEVICE_KEY="你的设备密钥"
```

采用 [Bark 官方 JSON POST](https://github.com/Finb/bark-server/blob/master/docs/API_V2.md)，密钥不进 URL 或日志。
同一比较键 24 小时去重；发送超时/结果不确定会记录 `UNKNOWN`，不自动重发，避免消息实际已送达后重复通知。
缺少密钥时不发送，也不占用去重窗口。所有 mock 报价强制 dry-run，即使 Bark 已启用也不真实发送。

每品牌并发固定 1；临时错误最多 3 次、随机延迟和指数退避；403/验证码持久化暂停，429 按等待时间暂停。
检查原因后才能显式 `python -m main --resume-brand hilton`；重启不会自动解除 403 暂停。
同一数据库只允许一个主程序进程，防止重复采集/复核。

## 真实希尔顿样本分析

最初两份国际站 HAR 得到 26 条有日期的房型起价；用户后来更新 `123.har`，补齐具体方案。
已核对江景大床豪华客房、2026-11-17 一晚、1 成人、1 间房、CNY 的 **5 条公开方案、4 条会员折扣**。
价格口径及区别详见 [完整方案报告](docs/HILTON_FULL_RATES_REVIEW_20261018.md)。

可离线复现（不会访问官网、导入 Cookie 或发送通知）：

```powershell
..\work\collector-venv\Scripts\python.exe -X utf8 -m scripts.analyze_hilton_har E:\Codexspace\www.hilton.com.har E:\Codexspace\123.har
```

结果保存在 `data/hilton-har-analysis.json`，同时区分房型起价与公开方案。重复导出的请求不会冒充独立抓取或二次确认。
历史样本不会自动写入正式监控库，也不会通知。

## 普通网页采价（需要明确选择 live 配置）

`config.hilton-live.example.yaml` 已配置真实酒店 SHAQTWA、1 成人、1 间房、一晚、单入住日，使用独立的 `data/hilton-live.sqlite`。
默认配置仍为 mock，不会自动运行此例子。需要实采验收时才选择：

```powershell
..\work\collector-venv\Scripts\python.exe -m main --config config.hilton-live.example.yaml --once
```

它会正常打开酒店日期页面、逐房型点击“更多房价”、读取页面本身产生的响应，不重放 GraphQL 请求，不注入 fetch，不复制用户浏览器资料。
正式历史仅比较明确公开非会员方案的原始金额和相同税费口径；保留方案代码、退款/预付条件，会员折扣分开排除。
原始金额不含税时会标注 `TAX_EXCLUDED`，含税显示另作参考，不能宣传为结算保证。

若已有采样工具的 403 暂停标记，该例子也会保持暂停。只有你检查原因并明确使用 `--resume-brand hilton` 才恢复；原标记归档保留。
代理只使用 `browser.proxy` 中你明确设置的现有出口，不自动轮换。浏览器、上下文与监听器均由本项目自建自清理，不接管原有 Chrome 或 Container Manager。
当前解析仅验证 CNY、单房、单晚；其他币种会明确提示未适配，不猜币种或含税口径。

## 目前未完成

样本已齐，不用继续导出同类 HAR。网页适配通过了离线浏览器回归，但还没有证明这台机器能自动通过真实官网访问；之前记录的 403 不会被静默解除。
下一步是明确恢复后的单入住日实采验收；没有验证真实 Bark 投递，也没有完成所有国家、币种和一年日期的覆盖。
万豪旧国际站单次诊断遇到的 403 标记仍在；新的中国站自动队列已取得真实价格并入库。
尚未完成全球酒店目录、所有国家/日期/房型覆盖和手机通知实投验证。
IHG、Hyatt、GHA、雅高仅预留接口。旧 NAS 项目没有部署本目录。

单元及浏览器回归测试拦截外部请求，Bark 测试使用本地模拟传输，不发送手机通知。
万豪中国站的真实查询另有本机 captures 文件及 SQLite 记录；离线测试不冒充官网实采。
