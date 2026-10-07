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
雅高和 GHA 的目录任务轮流提交，每次默认最多 1 家；当前新自动详情任务优先级70，低于关注85/近期80、保留48小时等待提升机制，控制访问频率。早期版本使用90，已在2026-10-05统一六集团目录优先顺序，避免大量收录任务抢占查价。
当前官网索引列出 6 个子目录，其中 1–3 返回 200，4–6 返回 404；404/410 记录在 `catalog:gha.missing_maps` 并继续后续目录。可读目录的酒店页及酒店活动页父路径合并得到 825 个去重候选，尚未逐一验证，不能宣称 825 家已抓取成功或全量覆盖。
详情页 URL → 正式 Worker 发现 → 查价 → SQLite 入库实测成功，样本 Avani Sukhumvit Bangkok Hotel 保存 3 条真实报价。目录重启续跑、去重和缺失子目录继续执行均有自动测试。

### 2026-10-01 新品牌回归：Capella Taipei

本轮真实读取官网 sitemap 索引和其中 `sitemap-3.xml`，该页 1000 个公开链接中包含
`https://www.ghadiscovery.com/ultratravel-collection/capella-taipei`。从此实际链接执行
隔离 Worker 酒店发现，确认 Capella Taipei、公开 booking hotelId `224544`。
首次 2026-10-20 / 10-21 查询有真实房型和含税价格，但整任务失败。
失败瞬间的公开 DOM 表明 Superior Accessible King Room 只显示 FROM / USD 1,189，
并没有其他房型的 NON-MEMBER RATES 标签；不能误报整站拒绝，也不能把 FROM 写成方案报价。

适配器现允许该总结栏提供房型与币种身份，仍必须展开 VIEW RATES，读取具体非会员方案、
币种和含税说明才保存报价。FROM 不入库、不用于降幅比较，会员计划仍排除。
修正后同一正式 Worker 流程成功：酒店发现 SUCCEEDED、报价 SUCCEEDED、17 条 PriceHistory、
Provider ONLINE、日历 AVAILABLE。测试独立临时数据库，无 Telegram 行情发送；不代表群晖已升级或 GHA 全目录已验收。

另移除“最多展开10次更多方案”的截断；分页以实际可见方案增加判断加载，不再假设手机重复节点始终为两倍。
分页未完成时报告超时，不把不完整报价集当作完整扫描覆盖旧日历。相关离线测试覆盖12次展开与超时失败。
新版再次真实查询 Capella Taipei 同日期，仍成功入库17条；美国 Viceroy Santa Monica
同为2026-10-20 / 10-21的正式 Worker 回归成功入库34条，Provider ONLINE、日历 AVAILABLE。

2026-10-01 全球目录自动选店链路追加实测：`smoke_sitemap_pipeline.py --provider gha`
正常读取官网索引与第一个子地图，由生产目录调度器自行选中
`https://www.ghadiscovery.com/adeera/atheel-kafd-hotel`，没有手工输入酒店代码或日期。
真实酒店资料任务确认 Atheel KAFD Hotel、hotelId 299035；随后生产 `expand_global`
自动生成2026-10-01 / 10-02的查价任务，正式 Worker 成功入库14条 PriceHistory。
目录阶段零报价，未向 Telegram 发送消息；隔离临时数据库，不是群晖安装或全目录采价验收。

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

## 2026-10-02 地图自动恢复补充（本地，未发布）

子地图暂时失败改为记录map_failures及重试时间，继续读取后续正常地图；
到期失败补采与新地图轮换，根索引失败时保留缓存地图继续推进。
401/403与429暂停目录请求并遵守等待时间，不以切换源绕过拒绝；集团暂停也作用于目录。
read_maps只记录真正成功项，失败及缺失数量独立展示，不凭游标位置宣称完整。
离线恢复与API验证通过，完整本地353项测试及前端构建通过，不是全球验收。
本次真实目录链路读取5个根地图链接、选中真实候选adeera/atheel-kafd-hotel，
资料任务在浏览器启动阶段失败BROWSER_UNAVAILABLE，没有新报价。
隔离本地浏览器七项运行测试随后成功，尚未据此解释该失败或宣布GHA实采通过。

### 2026-10-02 启动故障核对与恢复（本地，未发布）

同一GHA专用profile本地启动检查成功，未请求酒店网站；加入资料任务的脱敏异常原因诊断。
继续观察已运行的自动目录链路，未另起重复任务：资料与报价均SUCCEEDED，
自动选中Atheel KAFD Hotel（299035），自动入住日期2026-10-02 / 10-03，真实入库14条。
使用临时SQLite及模拟队列，配置来自数据库UI设置，无手工输入酒店，无真实Telegram发送。
先前失败原因仍未重现，不能将其归因于官网拒绝或声称已找到唯一根因。

代码核对另发现所有启动错误均被永久归为BROWSER_UNAVAILABLE，包括明确超时。
现在明确启动超时按TIMEOUT、OS临时资源不足按BROWSER_STARTUP_FAILED走既有有界退避；
缺失浏览器、权限错误、profile锁及未知启动错误不自动改为无限重试。
保留原profile、channel和代理，不清理会话或切换绕过；清理失败不覆盖原异常。
新增17项离线验证，相关31项通过，Ruff检查及99文件格式检查通过；没有重跑全部官网。
已成功抓取不再无变化重复测试，后续仅相关代码变更做必要回归，优先剩余集团与全局验收。

## 2026-10-05 持久全球目录续跑、新酒店真实价格与地区资料

使用`tests/audit_sitemap_catalog.py --provider gha`建立独立持久验收库，
生产目录服务/Worker、普通专用Chrome、已有代理和FakeRedis；不运行健康/投递，
不复制用户会话。已验Atheel、Avani Sukhumvit及Capella Taipei来源保存在排除名单，
后续进程不再重读根索引或成功详情，也不重跑已成功价格。

当前官方索引实际5个子地图：3个读取成功、4/5号返回404并保留missing_maps，
零失败地图，合并915个去重候选；旧825是历史时点，不当作当前数量。
持久游标已派发14项，其中1项为排除的Atheel、仍PENDING而非伪造成功，
另外13个此前未验详情任务均真实SUCCEEDED并入库。
这只是当前公开候选图和部分身份验证，没有公开全球酒店总数、不是全量覆盖证明。

目录正常全年调度自动生成Anantara Bazaruto Island Resort（7894）的
2026-10-05/06、1房2成人任务。`audit_ihg_price.py --provider gha`只执行此既有任务，
一次真实Worker成功（retry_count0、AVAILABLE），4房型/1命名方案共4条历史：
All Inclusive Luxury (BAR)，USD1902–3929/晚，公开非会员、明确含税费、nightly。
cash_price/tax未分解保持NULL，不把房型FROM汇总当方案价。
后续目录续跑保留4条历史，NotificationLog=0；没有真实Telegram或订单操作。

新详情任务的公开DOM证明官网页头有街道地址、城市/国家及Tel./View Map槽位，
不是没有地区信息，而是原适配器只保存预订页酒店名/ID。
现在从这一实际页头读取地址与明确城市国家，在公开预订身份确认后核对名称和官方路径，
匹配才合并；不从酒店名/URL猜国家，不从菜单、营销文案或电话推断位置。
未知区域/坐标/币种仍NULL，普通刷新不强制启用用户停用酒店。

真实新任务入库资料：Anantara Peace Haven Tangalle（5810）Sri Lanka/Tangalle、
Anantara Quy Nhon（6507）Vietnam/Quy Nhon、Anantara Santorini Abu Dhabi（152629）
United Arab Emirates/Abu Dhabi，均保存官网街道地址，可用于现有国家/城市关注规则。
实际Nice页头是`06 000, Nice, France`，已加入仅去除明确前置数字邮码的解析回归；
其他多逗号/歧义标签不猜地区。旧采集Nice/Rome仍只存街道地址、国家城市NULL，
等待正常刷新，不为新规则手工编造资料或重复已成功的价格。

验收工具另发现SITEMAP_MISSING导致错误停批，生产服务本身已记录缺失并继续。
工具现允许既有候选资料继续执行，缺失/失败地图独立报告，原缺失记录不清空或改成成功。
公开诊断仅投影酒店标题/地址/结构化位置字段，不打印Cookie、隐藏输入值或任意页面脚本。
新增15项离线地区/身份/诊断及缺失地图验收测试，相关155项通过（9.36秒），
Ruff及129文件格式检查通过；没有重跑完整797项旧套件或旧成功官网。

本次仍是Windows/独立SQLite/FakeRedis证据，不是最新Linux/PostgreSQL/Redis/NAS、
全球全年实采或真实机器人通知验收。没有Git提交/推送、CI、DockerHub或NAS更新。

## 2026-10-05 第二家新目录酒店自动报价（本地，未发布）

执行同一持久验收库既有全年调度任务db40f643-1863-4aba-9cc7-665bb3eebe86，
Anantara Angkor Resort（8103），2026-10-05/06、1房2成人。
真实Worker一次SUCCEEDED/retry0/AVAILABLE，9不同房型、1命名公开方案
Best Flexible Rate with Breakfast，共9条含税USD388–1284每晚total_price。
基础价及税分解未知NULL，不请求价格接口、手填日期/价格或重跑Bazaruto成功样本。
同库13酒店、13条价格历史（原4+新9），NotificationLog0；首次观察不声称已确认降价。
没有调用真实Telegram投递、Git提交/推送、CI、DockerHub或NAS更新；全集团/全年仍未完成。

随后执行另一个既有新酒店自动任务5336d04c-3096-49d0-8526-290feaa95d4c，
Anantara Convento di Amalfi Grand Hotel（133996），同样2026-10-05/06、1房2成人。
结果FAILED/PROVIDER_CHANGED，错误GHA room identity or inclusive summary price missing，
零历史/StayScan，没有确认是房型标题、金额格式还是含税摘要缺失，不能猜具体原因。
不把它写成HTTP拒绝、无房或零元；保留原失败，不立即创建新任务重复查询。

## 2026-10-05 Amalfi会员起价摘要修正（本地，未发布）

失败现场的公开卡片已定位：前两房型有NON-MEMBER RATES，但第三房型
Junior Suite with Views and Extra Bed只显示MEMBER RATES FROM EUR1877及含税说明，
View Rates正常可用。旧脚本要求摘要必须有公开起价，故中断整家酒店；不是HTTP拒绝。
现允许该摘要仅提供房型/币种身份，再打开真实方案。摘要金额绝不保存或用作基准，
DISCOVERY/会员命名或金色会员报价继续排除；没有公开方案的房型不产生公开报价。

保留原5336d04c-3096-49d0-8526-290feaa95d4c FAILED/PROVIDER_CHANGED。
一次明确代码修正复测ce0a29e3-bc07-40cb-95d1-b370aaf47e3a SUCCEEDED/retry0/AVAILABLE，
同2026-10-05/06、1房2成人，5个房型各一个实际公开命名方案
SPECIAL DEAL BREAKFAST INCL. BREAKFAST INCLUDED，EUR1497、2108、2370、3156、3418。
保存含税每晚total_price，未知cash_price/tax为NULL；会员摘要1877及该Extra Bed房型未入库。
专用库历史从13增至18，原有Bazaruto/Angkor历史保留，NotificationLog0，未真实投递。
补充成员摘要不当报价、币种/税费/标题守卫及会员房型不阻断其他公开房型的离线回归。
这不是GHA全球全年或生产Linux/NAS验收，代码和镜像仍未发布。

## 2026-10-05 会安新酒店自动报价（本地，未发布）

既有自动任务3e838beb-65cf-448e-871f-635149e57775，Anantara Hoi An Resort7433，
2026-10-05/06、1房2成人，一次真实Worker SUCCEEDED/retry0/AVAILABLE。
5房型/2公开命名方案Best Flexible Rates with BF、Hoi An Escape，共10条USD259–1134
含税每晚total_price；cash_price/tax未知NULL，member_rate false。
同专用库历史增至28，旧Bazaruto/Angkor/Amalfi成功价格不重抓，NotificationLog0。
不是已确认降价或一年全覆盖，没有调用真实Telegram或更新代码/镜像/NAS。

## 2026-10-05 普吉岛Layan新酒店报价（本地，未发布）

既有自动任务276daf87-0df1-412b-a6a8-a3f94c2eeefe，Anantara Layan Phuket Resort9317，
2026-10-05/06、1房2成人，真实Worker一次SUCCEEDED/retry0/AVAILABLE。
18不同房型/2公开命名方案Best Flexible Rate with Breakfast、Layan Residence Escape，
24条THB17212–881110含税每晚total_price；基础价与税分解NULL，全部member_rate false。
包括大套房/多卧室别墅，不把不同房型的高低差当成降价；只追加同报价身份的真实历史。
专用库历史52，NotificationLog0；首次观察不冒充已确认Bug，原成功酒店日期不重抓。
仍是开发机SQLite/FakeRedis证据，不是全球全年、真实通知或NAS验收；没有发布。

## 2026-10-05 后续目录与塞舌尔新酒店报价（本地，未发布）

新增Siam Bangkok、Stanley Livingstone、The Marker Dublin、The Palm Dubai、Ubud Bali、
Xishuangbanna六个官网详情均SUCCEEDED，累计19家/915候选，游标20。
原未观察自动任务f07f6943-4de0-40fd-b5b0-0db1a7e54629选择Anantara Maia Seychelles
Villas6890，2026-10-05/06、1房2成人，一次SUCCEEDED/retry0/AVAILABLE。
4房型/1公开Best Flexible Rate with Breakfast，4条EUR2875–4800含税每晚total_price，
cash_price/tax未知NULL；专用历史56，通知0。首次观察不是已确认Bug，其他成功任务不重抓。

## 2026-10-05 十个新详情及Desaru报价（本地，未发布）

Naladhu、Niyama、Qasr Al Sarab及七个Arjaan Rotana详情全部SUCCEEDED，累计29家、
915候选/游标30，旧三张成功地图和成功详情不重读。原未观察自动任务
37551ecb-ac36-40fe-8a09-1af7577a499e选择Anantara Desaru Coast Resort & Villas9125，
2026-10-05/06、1房2成人，一次SUCCEEDED/retry0/AVAILABLE。
13房型/3公开方案，21条MYR1469–37217含税每晚total_price，cash_price/tax未知NULL，
全部member_rate false，专用历史77、通知0。不同房型/餐食方案价格隔离，不当成已确认Bug。

## 2026-10-05 非酒店候选修正、拒绝分类与新日期报价（本地，未发布）

此前正常续跑增至49家/15个已知国家、915个原始地图候选、游标50，历史81。
下一条买D$条款页被误作为酒店，任务f79a21d7-1220-422c-8935-d3fd3c16d8c0因没有酒店
预订控件而TIMEOUT/PENDING/retry1。这是筛选错误，不是已确认反爬或无房。
实际缓存还含地区攻略、品牌福利和测试落地页，旧规则仅凭两段路径接受它们。

新增统一公共候选规则排除这些已观察非酒店栏目与条款末段，不使用品牌白名单，未知新
品牌仍可发现；真实酒店下的stay-offers/local-offers/experiences/dining映射回父详情页。
仅HTTPS官方主机、无用户信息/端口/query/fragment。新XML过滤；旧缓存不删除条目或移动
游标索引，只正常经过时跳过并记录。已排队任务访问前取消，不消耗集团网络请求名额，
保留原TIMEOUT、retry_count和scheduled_at；不删除原失败或价格。API按过滤后的去重
候选计数，原始游标经过非酒店页面不计为已派发酒店资料。

实际续跑由生产Worker取消原条款任务（仍retry1、原截止2026-10-04 23:21:58.653340 UTC）
后继续10个新详情，全部SUCCEEDED，累计59家/18个已知国家。源数组仍915项、游标61，
排除45个非酒店项，候选870、候选资料经过60项；不是870家酒店完成验收。地图仍3成功/
2缺失，旧成功地图及详情不重读，目录新增价格/通知0。

代码核对另发现search_hotels详情页将403/429统一记HTTP_ERROR，不触发集团暂停。
现与报价页统一401/403 BLOCKED_BY_ANTIBOT、429 RATE_LIMITED并解析秒数或HTTP-date
Retry-After，其他HTTP错误保持HTTP_ERROR。21项新增离线测试验证读取控件前停止、
错误头不泄露或改类别、Worker共享暂停以及后续任务不再请求，未人为制造官网拒绝。
候选/缓存/队列/API/续跑共30项新增回归，最新完整后端1037项通过（44.69秒）。

原未观察全年任务80069f5b-ee57-4152-a8a9-af2ccf9895ab选择Angkor8103的新日期
2026-10-06/07、1房2成人，SUCCEEDED/retry0/AVAILABLE。10房型/1公开Best Flexible
Rate with Breakfast方案，10条USD315–1284含税每晚total_price，cash/tax未知NULL，
member_rate false；专用历史81增至91、通知0。不同于此前10-05/06的9条记录，不重测
旧成功日期或将不同日期的金额差当同条件降价。没有真实Telegram、发布或NAS操作。

## 2026-10-07 新日期报价及居民限定价（本地，未发布）

持久目录续跑Centro Olaya与Centro Shaheen两个新详情成功，累计61家；候选870、
游标63、原始915项/排除45不变。原全球日期任务7d2ad8fa自动选择195721，Anantara
Stanley & Livingstone Victoria Falls Hotel，10-07/08、1房2成人，一次成功保存5条
USD775–1595含税每晚total_price；cash/tax NULL，历史91增至96、通知0。
其中3条Best Flexible Rate plus breakfast为USD1035/1165/1595，另2条明确SADC Local
Residence Special Including Breakfast为USD775/873，不能当普通公开低价发Bug通知。

新增RESIDENT_RATE只匹配明确居民优惠方案标签，不猜酒店名、房型或不透明rate_code；
报价及原身份/历史保留，API/UI说明居民限定，降幅检测和独立复查均排除普通提醒。
实际5条旧入库记录只读核对后为3条可参与普通检测、2条排除，没有重抓官网或发送通知。
这是特定样本/日期，不是全球全年完成；真实机器人和NAS验收仍未完成。

## 2026-10-07 一年窗口末端实际报价（本地，未发布）

沿持久游标10个新详情全部成功，酒店71增至81；870候选、有效游标84，915原始项、
45排除项、3成功/2缺失地图不变。没有重读此前成功酒店或把候选算作已收录。

显式--year-boundary从已有全球任务选中166420，Avani Ratchada Bangkok Hotel，
新条件2027-10-06/07、1房2成人。普通Worker任务dafbdf1d-6cfa-4416-b05c-77781e59db96
一次SUCCEEDED/AVAILABLE，保存40条THB3178–22363每晚含税total_price，14房型/4方案，
cash/tax未知NULL；历史96增至136，通知0。未修改原全球任务或推进/重置原日期游标。
首次观察不构成降价历史基线，不生成假Bug通知；单家末端样本也不证明全球全年全量。
完整后端1219项通过（58.17秒），未发布或更新NAS、未调用真实Telegram投递。
