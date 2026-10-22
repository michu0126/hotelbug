# 万豪被动采集与本地样本适配

2026-10-20：已新增[自动入库、降价检测和通知闭环](PASSIVE_MONITOR.md)。新监听器保存独立事件标识，
同价的不同请求不再按内容丢弃，以便二次确认。以下关于内容去重的描述仅对应旧版/无事件标识样本。

已接入用户提供的 `passive_capture.py` 和脱敏 JSON；测试不再依赖原始 `123.har`。
它直接连接本机 Chrome 的调试端口，不通过 Playwright 启动生产浏览器，不导航、点击、
注入脚本、重放接口或创建购物车。`--launch` 只负责启动独立 Chrome 并打开指定首页；
之后酒店、日期、查看房价都由用户正常操作。

## 本机直接运行

本次已在现有 Python 3.11 环境补齐 `websockets`、`tzdata`，在项目根目录执行：

```powershell
.\work\collector-venv\Scripts\python.exe -X utf8 .\collector\passive_capture.py --launch
```

普通安装则使用 `python -m pip install -e "./collector[dev]"`，再运行
`python collector/passive_capture.py --launch`。

在新开的 Chrome 中正常查价，收到目标200响应后保存到根目录 `captures/marriott/`。
profile 固定为 `collector/.profiles/marriott-passive`，首次为空，以后复用。
不复制日常 Chrome 或此前自动化测试 profile。需要登录时由用户在该窗口自行登录。
Ctrl+C 断开监听后，由用户关闭这个 Chrome，调试端口才会关闭。
不要在开启调试端口的窗口访问不信任页面，也不要把端口暴露到局域网。

监听只允许两站：`www.marriott.com.cn`、`www.marriott.com`，HTTPS、精确房价路径和POST；
相似域名、其他操作和重定向后的非目标响应不会导入。调试地址绕过环境代理，仅接受指定本机端口。
403/429等非200只提示并丢弃，不查询响应体、不发重试，也不修改原自动采集器的品牌暂停。

采集文件与 profile 均被 Git 排除。本轮增加响应体递归脱敏、嵌套敏感容器整体替换、
严格来源匹配、文件名字段校验及断线清理；拒绝仍带 JWT/邮箱的输出。
去重范围是当前监听进程内的同来源、变量、响应内容；不同 offset 的页分别保留。
这不是“能识别所有任意格式秘密”的保证，仍不应公开未经检查的原始 HAR。

## adapter 输入

`await fetch_prices(hotel_id, checkin, checkout)` 默认读取根目录 `captures/marriott/*.json`，
兼容以下字段：`source_site`、`operation`、`captured_at`、`property_id`、`check_in`、
`check_out`、`variables`、`response`；也保留旧 `samples/marriott` 输入。
页面来源会保留在 `source_url/source_site`，中国站和国际站不会混入同一比较键。
酒店、日期、房间数及人数与请求交叉核对，公开与会员价分开，货币和金额精度按原字段读取。

本轮提供的脱敏文件有75个完整节点；去重后67条：公开45、会员22。八组重复仅产品ID尾部不同。
它们仍是历史样本，不标成刚刚抓到的 LIVE，不写入正式 `quotes` 或触发 Bark。

采集器保留 `offset/limit`，但适配器目前只接受 offset0 的完整响应；
分页未齐时明确报错，不把不同时间的页面拼成完整结果或“无房”。

## 所选方案的税费、取消政策与 free_cancel_now

新增纯离线 `apply_checkout_evidence`，仅在站点、酒店、方案、房型、入住/离店、人数、房间数、
币种和会员资格都相同时，挂接已经观察并核验的所选方案补充证据；金额还必须能精确对账。
同时校验 `price_response_hash`，必须绑定同一份已观察的价格响应，不能仅凭同酒店、同价就挂接旧政策。
输入格式是 `marriott_selected_checkout_v1`，不含账户、付款方式或购物车标识。
可通过 adapter 的 `checkout_paths=[...]` 显式提供，或放进 `captures/marriott/checkout/`。

`tests/fixtures/marriott_cn_checkout_selected_sanitized.json` 是此前本地HAR分析的白名单投影，
不是新付款请求：NYCTE / RMOC / GENR / 2026-10-18至19日 / 1成人 / 会员，
417 + 45费用 + 71.64税 = USD533.64。其余66条报价不会套用这份税额或取消条件。

`record.free_cancel_now(as_of)` 返回 `True / False / None`：

- 明确不可退款，或明确免费取消截止已过：`False`。
- 条款有准确截止时间且未到期：`True`。
- 只有原始日期标记、营销名称、缺少时间/酒店时区等证据：`None`，不猜。

真实样本条款为“酒店当地时间夜间11:59…2026-10-15”，结合显式的 NYCTE 酒店时区
`America/New_York`（非从原始00:00Z推断），得到 UTC 2026-10-16 03:59。
在本次历史样本的采集时点已过期，尽管 `nonRefundable=false`、名称带“灵活退改”，仍不能免费取消。
非退款标志与完整政策分开保存，不简单取反当“当前免费可退”。

当前被动采集器只收房价响应，**不自动生成 checkout 补充包**。若用户没有另外提供对应证据，
`tax_amount` 以及准确的免费取消判断会保留未知，不靠创建购物车补齐，也不会把单个方案条件应用到全部报价。

## 验证边界

已用本机实际 Chrome 验证调试端口连接、Network订阅、读取响应、脱敏落盘。
该测试的两次页面请求全部被本地样本拦截；仅测试驱动操作测试页，被动采集器没有发页面控制命令。
保存结果在临时测试目录，不进入真实 captures 或价格历史。
这证明连接/监听能工作，**不代表真实中国站采价已经成功**。本轮没有访问真实酒店、解除自动暂停、
发送通知、推送代码或操作 NAS。

本轮最终验收：全部214项测试通过，Ruff通过；原始HAR不参与这些测试。
