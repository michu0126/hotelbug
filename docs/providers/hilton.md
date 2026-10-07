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

2026-10-01 后续正常窗口 Chrome 首页探测，经配置代理返回 HTTP 200，但标题、正文和控件列表均为空。
普通 Chrome 对照页也没有出现官网搜索控件；不能把这次 200 记为查价成功，也不能仅凭空页面分类为反爬拒绝。

2026-10-01 英文正常首页搜索对照：普通 Chrome 从英文首页搜索确切的 Conrad London St. James，选择 2026-10-20/21、1 间房、2 成人，再从正确酒店结果点击 View Rates，到达英文 rooms/rates 页面。实际大床豪华房（K1D）公开方案为 LV0 GBP595、R3X GBP584、PR09AP GBP546、PR09BB GBP631、B3F GBP619、CX09AP GBP581。GBP519 位于独立 Honors 会员列，不能作公开价。较前一天 GBP548/583 的金额已变化，测试分别保留各自观察投影，不能把它们当作新的自动入库结果。

英文可见日期摘要为 `October 20, 2026 through ... October 21, 2026, 1 room for 2 adults`，同一解析器现支持英文和繁体摘要，但必须完整匹配一种语言，不能把两个语言的片段凑成有效住宿。每晚价格与含税说明也按实际英文文案核对；币种从 `#selectCurrencyConverter` 当前值读取，方案仍只取 standard 公共列。提前预付方案未明确写早餐时保留未知，不根据标题制造早餐条件。

后续本轮普通 Chrome 回归：首页正常显示，确切酒店搜索异步返回两个建议，正确选择目标酒店，日期/两位成人控件均正确；但提交没有产生搜索结果弹窗。页面存在 Cookie 脚本错误，不能证明这就是提交无效的原因，也没有证据表明本轮搜索到达报价页。只记录实际结果，不继续重复提交。

本地未发布实现已加入目录酒店的正常首页流程：确切名称建议 → 官网日历逐月选入住/离店 → 读取当前成人数再调节 → Find a hotel 弹窗 → 按准确酒店代码查找结果卡 → View Rates 弹窗 → 住宿摘要/房型/方案校验。默认使用普通有窗口浏览器（Docker 由虚拟显示承载）。没有酒店名称的代码级诊断仍保留已验证的 deeplink；正常首页失败不会自动跳转 deeplink 重试。首页、结果、房型页均在任务结束后关闭，独立浏览器会话保持原有复用规则。

新流程真实独立 Chrome 实测（已配置代理、专用持久化目录、LONCOCI 2026-10-20/21）：在英文首页进入 Hilton Page Reference Code 错误页，零报价，未到达搜索控件。输出未提供本次主文档 HTTP 状态，不额外推断 403。此次证明流程仍未解决独立采集限制，不能把英文投影测试或正常用户浏览器历史成功算作 Worker/NAS 验收通过。

本轮价格口径修正（覆盖上述旧实现的税费推断）：`Prices include taxes` / `價格含稅` 不足以证明全球所有酒店已计入度假村费、目的地费等强制费用，因此公开金额保存为每晚 `cash_price`，`total_price` 保持未知。明确未含税的页面也可保存其真实每晚展示价，不因为不是全费用总价而丢失酒店报价。日历与 Telegram 沿用已有“官网展示价，全部税费未确认”标记；历史比较只与同报价身份、同 cash 口径记录进行，不混用旧 total 口径。

## 2026-10-02 地图恢复补充（本地，未发布）

全球地图扫描不再因单一子地图500/网络失败固定卡住后面的正常地图。
失败项持久化待补采，与未读地图交替；根索引刷新失败时继续缓存中的已知地图。
401/403及429按等待时间暂停目录，不立即切换源绕过拒绝；集团暂停也作用于目录。
API/UI分别显示真正成功地图、缺失项与失败待补采，不把游标推进当成成功读取。
离线恢复测试、完整353项本地回归及前端构建通过；本轮未重新获取希尔顿真实报价，
不能据此认定独立官网会话访问或全球全年采集已经解决。继续未发布。

## 2026-10-05 持久目录复核（本地，未发布）

沿既有专用Chrome/代理进行一次正常低频复核，验收库保存在
`work/hilton-directory-audit-20261005/catalog-audit.sqlite`。根索引保存542个酒店子地图，
之后另一进程按保存时间读取首图，解析5个候选并自动派发ATLAQAQ酒店详情。
任务35c12584-a9ed-4bed-859a-9f8736989f46返回
`BLOCKED_BY_ANTIBOT / Hilton Page Reference Code error page`，零酒店、零报价、零通知。
输出未提供此次主文档HTTP状态，不能额外写成403。集团暂停保存至
2026-10-05 10:34:46（Asia/Shanghai），没有重置冷却或继续切换酒店。
本次地图可读不是浏览器详情/报价成功；未重复已有成功集团的实采。

进一步修正地图拒绝与浏览器队列的暂停不同步：GHA/Hilton及Accor的地图401/403/429
现在同时持久化共享ProviderStatus，不再派发缓存酒店，已排队浏览器/全年调度也等待
同一时间；普通500/网络错误仍可沿缓存补采。已启动任务完成不能清除或缩短并发新暂停。
这些是离线回归证明的调度修正，不是希尔顿官网访问已恢复，仍未发布或更新NAS。

## 2026-10-07 详情恢复及价格导航失败（本地，未发布）

原暂停已经过去，正常续跑读取第二张地图，累计21候选、游标2。2133c5f0真实详情任务
成功保存AUSAQAQ，Placemakr Austin Downtown, Apartments by Hilton；国家未知保持NULL，
这是首个真实酒店资料，不是价格通过。没有更换代理/浏览器通道或复制用户Cookie。

原全球日期调度生成e9107a20，2026-10-07/08、1房2成人。三次均按原截止正常执行，
前两次TIMEOUT/rooms page，新增同页公开控件/阶段诊断后第三次明确TIMEOUT/
home navigation、UNCOMMITTED_BLANK（首页导航尚未提交）；retry3，下一截止
2026-10-07 05:45:45.432500 UTC，零PriceHistory/StayScan/通知。不能归因于房型选择器、
官网无房或认定DNS/代理具体故障，也没有直接改走另一个报价地址。

生产错误现区分首页导航、搜索输入、酒店建议、日历、人数、搜索提交、目标卡片、
报价页跳转及摘要加载；诊断只读已有项目页面，不发额外请求。新离线测试保持原错误、
空白/外部页面不读取内容、同一page去重及导航超时不请求备用地址。仍未取得真实报价。

## 2026-10-07 主文档状态证据（本地，未发布）

原e9107a20在原截止已过后第四次正常执行，新增--navigation-diagnostics，同一个项目
页面/en/主文档STARTED/RESPONSE200/COMMITTED/FINISHED，随后相同路径再出现
STARTED/RESPONSE403/COMMITTED/FINISHED；最后公开h1 SOMETHING WENT WRONG、
Page Reference Code。不是把“没有记录到网络事件”猜成网络故障，也不推断第二次导航
的具体触发者、IP或DNS原因。只有首页文档，没有可用报价页。

原任务FAILED/BLOCKED_BY_ANTIBOT/retry4，暂停至2026-10-07 12:30:39.527539 UTC；
零PriceHistory/StayScan/通知。之后没有请求该集团或改走另一个入口/代理/用户会话。
记录器仅监听既有项目页与正常popup的主文档，不请求额外页面；只投影固定公开路径、
状态码、net::ERR_*完整代码，丢弃query、子frame、XHR、脚本及非公开路径，不读headers/body。
64事件上限、每页一次挂载；关闭/失联或诊断出错不替换原查价结果。新增20项离线测试，
最终完整后端1205项通过。诊断能力及酒店详情成功均不代表自动报价成功。
