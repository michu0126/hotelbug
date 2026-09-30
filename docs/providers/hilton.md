# Hilton 研究记录
状态：Worker 适配器及全球官网目录已接入；普通 Chrome 已读到真实报价，独立浏览器实采仍未通过。官网入口：https://www.hilton.com/。
2026-09-29 实测补充：通过配置的 HTTP 代理，安装版 Chrome 的全新自动浏览器能加载英文首页（HTTP 200）。首页存在 `input[name="query"]`，输入 Hilton London Paddington 后实际 DOM 的 assertive live region 显示 `No results found`，未出现 option，并非已证实的定位器错误，也不能据此认定整站拒绝访问。另一轮未等到搜索输入框；仅凭超时不足以分类反爬。测试脚本 `backend/tests/probe_hilton_flow.py` 保留各阶段状态和页面诊断。尚未获得真实房价；下一步验证 Cookie 提示异步出现及搜索输入时机，并比较城市搜索与酒店搜索。
2026-09-27：官网英文酒店sitemap返回200；MLEHICI日期预订页返回403 Hilton Page Reference Code。
路线：正常搜索交互→实际网络请求→同酒店同日期现金/积分报价fixture→字段映射；受阻标BLOCKED。
不能基于其他项目示例猜测GraphQL Endpoint。
2026-09-29 后续验证：城市 London 逐字输入后同样显示 No results found，但日历控件可打开，显示明确的入住/离店日期按钮。发现并修正探测脚本 Cookie 按钮定位错误：可访问名称为 `dismiss cookie message`，不是可见文本 `Do Not Accept`。修正后的新会话首页初始响应 200，随后出现 `Hilton Page Reference Code / SOMETHING WENT WRONG`，未到达有效报价结果。此轮不能证明 Cookie 修复解决了查价；也没有证据证明日期搜索提交成功。不连续重试同一失败会话。
Endpoint/Method/Payload/Headers/Cookie/Token/价格税费及积分字段均未验证。
目录可读与报价可读分别记录；积分和现金分开保存。
2026-09-30：英文 sitemap 索引 `/sitemap/en/sitemap-en.xml` 返回 200，包含 989 个子地图，其中 542 个路径含 `prop`；首个酒店子地图 `sitemap-en-prop-hp-001.xml` 返回 200、107 条 URL，包含酒店主页与 gallery/rooms 等子页。Hampton Aachen Tivoli 酒店主页浏览器返回 200，显示酒店名称和日期、人数、Check prices 控件；在下一次操作前页面转入 `Hilton Page Reference Code` 错误页，尚未拿到可比较报价。目录虽能用于发现候选酒店，不能证明查价功能已完成。探测脚本已扩展 `--property-url` 参数。
另一家 sitemap 酒店 Hampton Inn & Suites Kutztown（ABEKZHX）主页和 rooms 介绍页也返回 200，主页日期/人数/Check prices 控件可见。正常点击 2026-10-07/08 的日期并提交 Check prices 后打开官方 `/en/book/reservation/rooms/?ctyhocn=ABEKZHX&arrivalDate=2026-10-07&departureDate=2026-10-08...`，但落到 Hilton Page Reference Code 错误页，无房型报价；不带代理的全新浏览器会话也在同一阶段转入错误页。不能把主页 200 当作报价成功，也暂不能判断是会话、路由还是站点策略导致。

2026-09-30 实际普通 Chrome 对照：LONCOCI（Conrad London St. James），2026-10-20/21、1 成人、1 间。官网繁体中文 deeplink 正常进入 rooms 页，8 个可售房型；点击大床豪华房的最低价按钮后进入 rates 页，公开 Flexible GBP 595、Semi-flex GBP 584、Advance Purchase GBP 548，另有早餐等方案。页面说明平均每晚价含 GBP 税；GBP 521 是会员提前预订价，不能当公开价保存。页面没有登录账户，不能因此把失败归咎于用户必须登录。

官网中国站能正常搜索全球城市酒店并选择日期，但伦敦列表在 2026-10-20 与 2027-09-28 显示同样起价（Conrad CNY 3075），无法证明是对应日期可售报价，故不能用于日期监控。搜索建议必须匹配城市精确名称和国家；仅 contains 会把“伦敦”误选为“阿伦敦”。实际预订链接转到 hilton.com。

同一 LONCOCI deeplink 在独立 Playwright、正常 Chrome 独立临时用户目录、直接出口/配置代理中仍落到错误页。独立正常 Chrome 先访问繁体首页（200）、停留后再开 deeplink，最终也为 Hilton Page Reference Code，虽初始 HTTP 200；因此“先暖首页”未解决该样本。普通 Chrome 成功说明官网报价与页面定位器可研究，不证明 NAS 自动会话成功。

真实 DOM：rooms 页的 `data-testid=roomTypeName`、`moreRatesButton`；rates 页的 `rateTableDescriptionCell`、`rateTableStandardCell`、`rateTableHonorsCell` 在同一 grid 中按方案顺序相邻。公开金额在 standard cell 的 `ratePrice`，会员金额在 honors cell 的 `honorsDiscountPrice`。应将 standard cell 与最近的前置 description cell 配对；币种从“選擇貨幣”的 select 当前值读取。日期/人数需核对 `search-edit-button` 的完整标签，不能仅检查 URL 或抓列表起价。

后续将同一普通 Chrome 查询改为两位成人，完整日期标签确认 2026-10-20/21、1 间房、2 成人。大床豪华房公开 LV0/彈性 GBP595、R3X/優享 GBP584、PR09AP/提前預付 GBP548、PR09BB/早餐 GBP631、B3F/優享含早餐 GBP619、CX09AP/提前預付含早餐 GBP583。会员提前预付 GBP521 仍在独立 honors 列。已实现 `hilton_page.py` 可见页面解析，14 项离线测试覆盖逐方案配对、公开/会员隔离、日期年份、人数、酒店/房型、含税夜价和原始 rooms URL 关联；尚未注册为自主 Worker 适配器。两位成人的 rooms URL 在独立采集浏览器本次导航超时，不能据手动浏览器成功声明自动链路通过。

2026-10-01 当前实现：`hilton_browser.py` 已注册到 Worker。按确切酒店代码、入住/离店日期、成人数进入官网 rooms 页，等待完整日期/酒店摘要；从 `roomCardTile` 的真实 `data-roomtypecode` 读取房型（大床豪华房为 K1D），逐个点击查看方案，读取 standard 列公开价，再通过更改客房返回下一个房型。不点击最终预订/付款按钮。只接受官网明确含税的单晚公开方案，会员价不混入。页面加载未完成和 HTTP 200 的 Page Reference Code 不视作成功。

真实独立 Chrome（专用持久化目录、已配置 HTTP 代理）运行 `smoke_hilton_browser` 查询 LONCOCI 2026-10-20/21，本次返回 HTTP403 / Hilton Page Reference Code，尚无自动报价入库实证。浏览器导航接缝的离线测试验证两房型各四个公开方案可入库和显示日历；该测试使用本地投影 DOM，不是新的官网实采成功。

官网目录真实回归：索引中筛出 542 个英文酒店子地图；首个子地图解析到 5 个酒店主页，排入一个正式 DISCOVER_HOTELS 任务，候选为 Scout Living Atlanta（ATLAQAQ）。地图和酒店派发游标保存于数据库，断点续跑、重复链接、过滤 rooms/gallery 子页、默认关闭不发请求均已覆盖测试。地图读取按每次至少 30 秒推进；候选只有完成官网详情验证后才计入酒店数据库。启用需 `HILTON_ENABLED=true`，但开关不证明实采成功。全年日期轮询和降价/Telegram 链路复用现有服务。

ATLAQAQ 的真实隔离 Worker 目录任务随后运行结果为 PENDING/TIMEOUT，没有酒店记录，因此不能把前述五个候选当成成功收录。包含希尔顿页面/Worker/目录在内的本地离线测试共 114 项通过；真实网络限制仍需解决。
