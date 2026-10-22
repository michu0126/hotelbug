# 万豪 PriceRecord 样本适配器

## 本轮范围

`adapters/marriott.py` 提供异步 `fetch_prices(hotel_id, checkin, checkout) -> list[PriceRecord]`。
默认仍为**离线样本适配器**；显式 `mode="live"` 可启用独立官网浏览器实查。
主监控程序的万豪批量任务和 Bark 仍未启用。旧 NAS 项目、Cookie 均不修改。
2026-10-18 首次实查返回 403，已经持久化暂停；具体结果见下方“单次实采”。

用户本轮提供的 `capture_marriott.py` 和 `find_price_endpoints.py` 是抓包/定位工具，不包含实际价格响应。
本轮复用项目已有的 `backend/tests/fixtures/marriott_observed.json`，复制价格部分至本目录测试样本。
证据是纽约万豪 Marquis（NYCMQ）2026-10-07 至 10-08 的会员方案 AP0K；
未税 USD 1152.00、含税费 USD 1377.06、单列强制费用 USD 45.00。
原始请求与精确采集时间未随该 JSON 保留，所以返回的 `adults`、`captured_at` 均未知，不能伪造为当前时间。

## 字段口径

| PriceRecord 字段 | 来源 / 规则 |
| --- | --- |
| `member_rate` | `basicInformation.isMembersOnly` 的明确布尔值；缺失报错，不靠方案名称猜测 |
| `amount_excluding_tax` | `totalPricing.rateModes.subtotalPerQuantity.amount`，单间整段住宿未税总价 |
| `amount_including_tax` | `totalPricing.rateModes.grandTotal.amount`，单间整段住宿含税费总价 |
| `average_nightly_excluding_tax` | `rates.rateModes.averageNightlyRatePerUnit.amount`，与住宿总价分列 |
| `currency` | 每个金额自带币种；必须一致，不猜 USD、不自动换汇 |
| 金额精度 | `Decimal(amount).scaleb(-decimalPoint)`，支持零位和三位小数，拒绝缺失精度 |
| `mandatory_fees` | `totalMandatoryFeesPerQuantity.amount`，缺失保留未知 |
| `tax_amount` | 当前未知；含税费总价减未税总价包含费用，不能宣称是纯税额 |
| `cancellation_policy.free_cancellation_until` | 已有官网查询选择的 `basicInformation.freeCancellationUntil`，有则保留原文 |
| `cancellation_policy.refundable / policy_text` | 真实样本尚无明确证据，均保留未知；“Non-refundable”名称不当作完整合同 |
| `rate_description` | `rates.description` 原文，不把整段营销描述冒充取消条款 |

现有真实样本只验证了会员价、USD 和单晚。公开价、其他币种、两晚总价及取消截止字段的
测试使用**明确的合成变体**覆盖分支，不宣称这些条件已经官网实采验收。
酒店、日期、房型和方案通过响应产品 ID 核对；请求元数据存在时也核对入住日期、单房及成人数。
积分/现金加积分不冒充现金；部分分页、不同币种、冲突报价、403/429 和非 JSON 不变成“无房”。

## 使用方式

把 `capture_marriott.py` 导出的 JSON 响应放在 `collector/samples/marriott/`。
它的 `url/method/status/request_post_data/response_text` 包装可以直接读取，无须保存 Cookie 或重放请求。
模块函数只返回该酒店和日期匹配的样本记录；没有匹配样本会明确报错，不返回假空列表。

在 `collector/` 中调用：

```python
import asyncio
from adapters.marriott import MarriottAdapter
from storage import Storage


async def example():
    adapter = MarriottAdapter(["tests/fixtures/marriott_observed.json"])
    records = await adapter.fetch_prices("NYCMQ", "2026-10-07", "2026-10-08")
    db = Storage(":memory:")
    try:
        for record in records:
            db.save_price_record(record)
        members = db.price_records("marriott", "NYCMQ", "2026-10-07", "2026-10-08", member_rate=True)
        print(members[0].to_dict())
    finally:
        db.close()


asyncio.run(example())
```

不指定路径时，使用 `from adapters.marriott import fetch_prices` 后
`await fetch_prices("NYCMQ", "2026-10-07", "2026-10-08")`，读取上述默认样本目录。

公开价和会员价共用 `price_records` 表但有独立字段、索引和比较键，按 `member_rate` 分开查询；
同酒店同房型同方案也不会合并。金额以十进制字符串存储，重复 JSON 导入不会增加独立记录。
本地回放永远标记为 `SAMPLE`；官网实时响应才标记 `LIVE`，且必须有采集时间及明确人数。
两种来源使用不同比较键，均不能自动写入 `quotes` 通知监控历史。
每次回放可能返回多份历史响应；调用方不得把它们当作“刚刚实时查询”的价格。

测试：`python -m pytest tests/test_marriott.py -q`；整套：`python -m pytest -q`。

2026-10-18 本地验收：万豪专项 48 项、采集子项目全部 170 项通过；Ruff 和编译检查通过。
真实样本的价格响应已与旧项目文件逐字段比对相同。没有真实酒店请求、通知投递或 NAS 部署。

## 首次单次实采（历史流程，已被下节替换）

`MarriottAdapter(mode="live", browser_settings=BrowserSettings(...))` 会正常打开用户提供的
`/reservation/availabilitySearch.mi` 起始页面，仅观察网页自身产生的已知房价响应。
实采失败不回退到旧样本，不重放接口，不注入 fetch，不拷贝已有浏览器 Cookie，不自动切换出口。

已执行一次：NYCMQ，2026-11-17 至 11-18，1 房 2 成人；普通有头 Chrome，
代理 `http://192.168.50.4:7890`。官网入口先返回 200，随后同一入口返回 403；
未得到已验证房价，保存价格数为 0，没有通知或 NAS 操作。
证据：本地忽略的 `data/marriott-live-probe-20261018.json`，暂停库 `data/marriott-live.sqlite`。

本次还发现迟到 403 的状态竞态：最初报告为 TransientFailure，随后清理阶段才收到 403。
已经修正为清理后仍传播该拒绝，并增加报告/暂停一致性检查；原始 FAILED 结果保存在
`original_outcome`，实际观察的 403 不变，只补记持久化暂停。当时未自动重试；新的明确授权测试见下节。

以后有明确恢复授权并复核暂停原因后，才可进行新的实查。目前不会因重启自动恢复。
单次诊断入口（当前暂停库下只会报告暂停，不会再访问官网）：

```powershell
..\work\collector-venv\Scripts\python.exe -m scripts.probe_marriott --hotel NYCMQ --checkin 2026-11-17 --checkout 2026-11-18 --adults 2 --proxy http://192.168.50.4:7890
```

此入口每次最多一次采集尝试，沿用随机请求间隔；403/验证码持久化暂停，429 按 Retry-After 暂停。
成功报价可写 `price_records`，公开/会员分类保留；它不是全年轮询或降价通知开关。

最终回归：万豪相关 60 项、采集子项目全部 182 项通过，含清理阶段迟到403的独立复现、
暂停跨运行保留、无样本回退及 LIVE/SAMPLE 隔离；Ruff 检查通过。以上回归全部离线。

## 当前流程：持久化 profile + 首页交互

按用户明确要求修改：

- `launch_persistent_context` 使用固定的 `collector/.profiles/marriott`，本机 Chrome、有头模式。
- 每次重开复用该目录，而不是复制日常 Chrome 的个人资料；测试验证 Cookie 可跨关闭/重开保留。
- 首页 `https://www.marriott.com/` → 目的地搜索 → 酒店页面的真实链接 → 日期控件 → 查看房价。
- 不再直接导航 `availabilitySearch.mi` 或拼接查价 URL；不注入 fetch，只监听网页本身的响应。
- 只有完成日期选择并点击查看房价后，才接受匹配酒店/日期/人数的价格响应。
- 403/429/验证码取消正在等待的页面操作并暂停；不清空 profile、不换出口或入口。

profile 已从 Git 排除，独立锁阻止两个进程同时占用；关闭的仅是本工具自己启动的上下文。
不退出日常 Chrome，不操作 NAS 或 Container Manager。

2026-10-18 23:49（北京时间），用户明确授权后只执行了一次新流程：NYCMQ、
2026-11-17 至18日、1房2成人，代理仍为 `http://192.168.50.4:7890`。
首页 `/` 返回301到 `/default.mi`，该页先200后403。在目的地搜索尚未完成时停止，
没有进入酒店页面/日期选择，0条价格、0通知。不能宣称完整真实流程已验收。
profile 首次创建并保留，未导入日常 Chrome 的登录状态。

新证据：`data/marriott-profile-homepage-probe-20261018.json`，原首次记录保留不覆盖。
`explicit_resume.prior_pause` 记录此前暂停；本次再次403后，同一个 SQLite 库已恢复暂停。
`--resume-brand` 只在用户明确要求恢复时使用，普通重启/运行不解除暂停。
后续没有再查询官网，未推送代码或部署容器。

当前流程回归：万豪相关62项、全部184项通过，验证持久化目录复用、关闭/重开Cookie、
首页点击链、固定有头Chrome启动参数、暂停恢复审计和拒绝后停止；这些回归均使用本地样本。
