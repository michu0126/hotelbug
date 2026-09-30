# GHA DISCOVERY 研究记录
## 2026-09-29 官网页面实测（当前实现方向）

不再以寻找私有接口为前提。安装版 Chrome 通过配置的 HTTP 代理，直接读取详情页和预订页，均返回 200。
Avani Sukhumvit Bangkok Hotel 的详情页 BOOK NOW 链接提供公共预订参数 `hotelId=10624`、`startDate`、`endDate`、`room1Adults`、`room1Children`。
把日期改为 2026-10-07/08 后，预订页实际显示 `Wed, 07 Oct - Thu, 08 Oct 2026`、`ROOM 1: 2 ADULTS`，三种房型有报价。
点击包含 checkbox 的可见 label 切换含税价格（直接 check 隐藏 input 会被 label 遮挡）。DELUXE KING 的非会员起价从 THB 5,900 变成含税 THB 6,944。
点击 VIEW RATES 后，可见具体计划 Avani Flexi、THB 6,944/night、Including taxes and fees；另外两个 DISCOVERY 会员计划为 THB 6,250 和 6,674，不应混入非会员基线。
DOM 中 `.tid-selectBtn` 的三层父节点包含计划标题 h5、价格 h5、税费文字、`.tid-viewTotalStay` 条款按钮；移动端重复节点需要去重。
`backend/tests/probe_gha_booking.py` 可重现实测，只查价，不选择房间、不提交预订。
已实现 `GHABrowserProvider` 并注册：读取每个房型的明确 NON-MEMBER RATES 含税起价，再打开具体计划，只保存同币种、正金额、含税且未标会员的可见计划。房型卡片的 “FROM” 是聚合起价，不要求它等于每一个方案价；若公开计划出现更低的异常价格，也不会因此被过滤。房型和计划用名称哈希标识，不伪造官网代码。按网页展示保存 nightly 价格，税额、取消和早餐字段未知时为 NULL。
真实 Worker 测试 `tests.smoke_accor_pipeline --provider gha --proxy ...` 已通过：发现任务 SUCCEEDED、查价任务 SUCCEEDED，3 条 PriceHistory，Provider ONLINE。此轮官网显示 USD 207/217/252，币种按实际页面保存而非固定 THB。
离线测试覆盖日期、人数、酒店 URL、会员排除、税费、币种和公开方案价。仍需验证其他品牌、跨年日期的页面格式、精确总价/条款及全量目录，不能据少数酒店样本声明 GHA 全量完成。

2026-09-30 跨地区实测：官网目录候选页成功确认 Erbil Rotana（伊拉克，hotelId 238788）、Viceroy Santa Monica（美国，5149）、The Leela Hyderabad（印度，196125）的酒店身份。Viceroy Santa Monica 2026-10-08/09 页面有售罄房型的禁用 VIEW RATES 按钮；原采集器误点击并超时，已改为只点可用按钮。该酒店非会员房型起价 USD 543，但可见的 Best Flexible Rate 含税报价为 USD 574/night；旧的“方案价必须等于起价”条件会丢掉全部公开方案。修正后读到 34 条公开报价，最低示例 Best Flexible Rate USD 574。Erbil Rotana 同日期读到 18 条，公开 Flexible Rate 起价 USD 217。另用隔离数据库从 Viceroy 官网详情页发现酒店，Worker 查价并写入 34 条 PriceHistory，任务 SUCCEEDED、Provider ONLINE。以上均为测试时的页面价格，不代表现在仍可预订或全目录已验收。
同一 Erbil Rotana 在 2027-09-28/29（仍处监控的一年范围内）页面明确显示 `Selection not available for these dates`，无房型列表。原脚本等待按钮 30 秒后误报 TIMEOUT；现在识别此官网状态并返回空报价，不入库零价，也不视作页面故障。隔离 Worker 的正式任务复测为 SUCCEEDED、PriceHistory 0、Provider ONLINE；同酒店 2026-10-08/09 的 18 条报价复测仍正常。

### 自动目录接入

2026-09-30 会话复用回归：hotelId 10624 对应 Avani Sukhumvit Bangkok Hotel（不是 Erbil Rotana，其 ID 是 238788）。2026-10-20/21 实际页面读到 35 条公开报价；示例 Avani Advance Purchase USD 92、Avani Flexi USD 102。随后从酒店发现任务到正式 Worker 报价任务的隔离数据库测试通过：SUCCEEDED、35 条 PriceHistory、Provider ONLINE、日历 AVAILABLE。专用会话的跨任务、跨重启和集团隔离亦通过本地真实 Chrome 测试。

Scheduler 已加入 GHA sitemap 索引遍历，每轮最多请求一个目录、持久化地图/酒店游标、按队列余量提交详情页发现任务。详情页读取 BOOK NOW 的数字 hotelId，再验证预订页酒店信息并入库，不直接将候选路径当作已收录酒店。
雅高和 GHA 的目录任务轮流提交，每次默认最多 1 家，发现任务优先级 90，高于普通查价 80/60/40；这样新酒店不会在全球日期队列积压时持续被压后，也控制单个集团的访问频率。
当前官网索引列出 6 个子目录，其中 1–3 返回 200，4–6 返回 404；404/410 记录在 `catalog:gha.missing_maps` 并继续后续目录。可读目录的酒店页及酒店活动页父路径合并得到 825 个去重候选，尚未逐一验证，不能宣称 825 家已抓取成功或全量覆盖。
详情页 URL → 正式 Worker 发现 → 查价 → SQLite 入库实测成功，样本 Avani Sukhumvit Bangkok Hotel 保存 3 条真实报价。目录重启续跑、去重和缺失子目录继续执行均有自动测试。

## 早期接口观察（历史记录，不是当前实现依赖）
状态：Python适配尚未实现；Phase6。旧Node页面采集器独立保留。
官网入口：https://www.ghadiscovery.com/；酒店详情→BOOK NOW→预订页。
2026-09-27开发机，NH Collection Dubai Ibn Battuta，2026-10-07/08，1间2成人。
观察官网浏览器实际发起 POST https://oscp.ghadiscovery.com/api/v3/booking/hotel/rooms/rates，返回200。
非猜测路径，但**只验证浏览器自然请求，未验证独立HTTP请求的会话/Token要求**。
Payload已观察字段：numberOfRooms、numberOfAdults、startDate、endDate、hotelCode、brandCode、chainId、hotelId、numberOfChildren、childAges、content、primaryChannel、secondaryChannel、loyaltyProgram、loyaltyLevel。
样本hotelCode=NHNIBN；页面hotelId=310679与报价payload hotelId=25307不是同一ID体系，不可互换。
Headers/Cookie/Token：没有保存或验证最小必需集合，不伪造“不需要认证”的结论。
返回rooms[].roomName/roomCode、rates[].rateName/rateCode、price、currency、memberRate、taxInclusive、amountWithTaxesFees。
示例会员price=405 AED、amountWithTaxesFees=516.13；非会员price=450 AED、amountWithTaxesFees=571.25。
税费可能可由一致口径金额差计算，但需验证多晚、取消政策、早餐和其他品牌，不能直接把dailyPrice猜成基础价。
points_price、refundable、breakfast_included未确认，NULL。
页面非会员起价聚合缺少严格Rate Plan身份，不应作为新架构高置信异常检测基线。
Phase6先研究直接HTTP可行性（JSON优先）；必要时复用浏览器。不能将一个品牌样本推广全GHA。
