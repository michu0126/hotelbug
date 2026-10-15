# Marriott 官网研究记录（Phase 2 实验版）

## 最新核验：2026-10-15（本地未发布）

真实月历 Worker 已写入 PBHMD 2026年12月31个日期的 USD522–546 起价，1房2成人1晚。
正常“更新搜索”会把地址货币改为 default 并移除 propertyCode；采集器现校验页面酒店
链接，保留实际打开的酒店专属入口作为稳定来源。月历没有 rewards 输入控件时，
使用明确的“1晚客房总费用 (USD)”识别现金，仍拒绝积分状态及不完整日期／金额。

新增 FETCH_CALENDAR_HINTS 和专用历史表，轮换未来365天涉及的月份。日历降价候选
需再取得公开非会员含税房型报价，并独立二次确认后才能入通知队列；容量满时保留候选。
来源变更、酒店停用和日期过期均会重新核验，跨午夜保留实际请求窗口并裁剪过期入住日。
UI 分别显示日历观察和正式房型覆盖，酒店日期格可查看起价和时间。

持久目录续跑第二／第三页真实成功，酒店共196家；当前官网总数10,437。
原第三页 hash 校验修复已获真实响应验证。首轮月历历史不足以证明真实降价通知，
未发真实 TG，未发布或部署 NAS。详见 [批量日历监控](../CALENDAR_MONITORING.md)。

## 最新核验：2026-10-14（本地未发布）

同一持久目录库正常续跑：台湾当前官方响应34/34，补1家至116家；既有16条报价
及零通知不变。中国第三页任务619c51e2校验失败，进一步同源诊断证明正常点击
页码3发出offset80，响应40家、pageInfo.currentOffset80，DOM81–120/855，但
实际网址hash是#/1/，不是逻辑页码#/2/。因此URL只验证官方来源及国家／过滤条件，
页码改由原请求和响应offset、完整酒店数、页面counter一致证明。保留对错误国家、
非官方来源、部分响应、陈旧offset、未知fragment形式和实际范围不一致的拒绝。
离线专项100项通过；修正后一次真实复核遇到HTTP500，尚无修正后的第三页成功实采。
原错误任务及目录失败状态保留，不直接改为成功。证据：
`work/marriott-cn-global-batch-20261014.json`、
`work/marriott-cn-page3-diagnostic-20261014.json`、
`work/marriott-cn-page3-fixed-20261014.json`。

同一项目独立普通Chrome自动选择尚无房型报价的目录酒店PBHMD（Le Méridien Thimphu），
沿现有官网日历控件设置1房2成人、1晚，再点击正常Next Month。2026年10月读取18天，
11月读取30天，共48个显式日期的价格提示；页面明确标注1晚客房总费用(USD)，
金额显示$617至$1,235。日期身份取单元格完整日期，不取翻月后仍留在10月的搜索摘要。
页面说明总房费是每晚费用下限总和，且大多数国家不含税费；无明确房型／方案／会员
身份，不能冒充房型含税RateData或直接发降价通知。本次仅保存公开页面证据，
无正式报价数据库写入，无TG调用，浏览器清理成功。
证据`work/marriott-cn-calendar-two-months-20261014.json`。

这证明单页日历可以批量预筛多个日期，但自动月度任务、提示价历史、候选异常日期
优先复查及完整报价确认仍未接成生产链路，不是全球全年采价验收。

## 2026-10-10（本地未发布）

真实 Worker 已完成全球目录自动生成的 PBHPR（Le Méridien Paro, Riverfront）
2026-10-10/11、1房2成人报价任务：16条公开含税报价入历史，7种房型，
USD612–1963住宿总价；任务、浏览器清理均成功，未调用 Telegram。
证据 `work/marriott-cn-auto-price-20261010.json`，同一持久目录库累计115家真实酒店。
这些记录只在开发机验收库，不是 NAS 入库或全球全年验收。

中国目录公布855个结果。直接打开 `#/1/` 仍返回 offset0、第一页1–40，是本次
超时的具体原因。现点击正常公开页码按钮，并核对请求／响应 offset、pageInfo 和
完整不同酒店ID。第一页／第二页都取得40/40家；第二页保存 `#/2/` 后续任务。
页面卡片在第二页仅能解析31家，而同次公开响应有40家；国家目录生产路径已
改读这份页面自身发出的响应，不再因没有“查看房价”链接而遗漏酒店。
`bookable=false` 仍保存酒店身份，但不据此填可售状态、日期或价格。
CN查询实际包含CN/TW/MO/HK，国家范围不冒充每家酒店地址证据。

已选目录源的自动健康探测使用该来源的实际酒店，不再固定探测英文NYCMQ；
普通到期价格任务优先于重复探测。新增全球／复查／失败补采任务限定接受的来源，
源变化后仅取消带自动来源标记的旧任务；未标记旧任务、手动检查、明确关注及
历史资料不被批量取消。当前已停顿的集团仍遵守原暂停期限。

以下保留此前阶段的单次证据，不能把临时库酒店数直接相加。

### 2026-10-08 / 09

以下是新增中文官网路径，不用历史英文站点失败结果概括当前状态。
`MarriottBrowserProvider` 仍是 Worker 工厂；明确的 `www.marriott.com.cn` 酒店链接
按该站真实控件操作，不在拒绝后自动轮换域名、代理或用户会话。

- SINNV，2026-10-30 至 10-31，1房2成人：项目专用普通 Chrome 成功读取全部7个
  已显示房型的非会员含税费每晚价，SGD 224/226/226/250/261/261/310。
  价格和浏览器清理均成功，证据 `work/marriott-sinnv-cn-all-rooms-20261008.json`。
  本次修复含税复选框应点击可见 label，以及懒加载房型不能停在最初两张卡片的问题。
  这是独立 Provider 实测，未写 NAS 数据库；不能直接称为 NAS/全球/全年验收。
- 中文新加坡目录 `findHotels.mi` 的正常列表公布22家。真实 Worker 在独立持久库
  自动入库21家酒店资料，并生成21个单晚任务；未执行这些报价任务或发送 Telegram。
  第一次读取过早遇到 `1-0/0` 加载占位，已等待有效范围再解析。
  仍有1张卡片未解析，所以目录保存 `PARTIAL / 21 of 22`，不是全量完成。
  证据 `work/marriott-cn-catalog-hydration-20261009.json` 及同名 SQLite。
- 酒店身份来自官网 `availabilityCalendar.mi` 链接的 propertyCode，保存原链接，
  不需要逐酒店手工填代码或猜详情 slug；这些链接已接入报价适配器的 URL 验证。
  目录起价不作为 RateData，也不据其发送降价提醒。
- 全球根目录的11地区 UI 有隐藏区域复用和异步切换问题。现读取正常官网页面自身
  发出的 `phoenixShopSearchPropertiesByLocationInput` 全球统计响应，不再依赖逐区
  展开。核验请求无地区/品牌过滤，151个国家／地区分组总数与响应总数相符；
  真实 Worker 已把151个国家目录入口入库。成功这次官网公布10,437家，前一份
  观察为10,435家，保留各自时间和计数，不写死总量。
  证据 `work/marriott-cn-global-verified-20261009.json` / 同名SQLite。
  随后从同库自动续跑首个不丹目录，2/2酒店 PBHMD、PBHPR 真实入库并生成日期任务，
  没有重读成功的根目录。国家分页随后于10月10日验证，见上。
- 管理端明确提交根目录才登记选源意图，成功后生效；旧自动任务晚完成不能改变
  已选源。旧源证据保留，但不混入当前源目录页/失败页计数。

同一轮英文站独立匿名 GraphQL 仍返回 Access refused，未继续发送其他报价请求。
中文全球页的统计响应已用于目录入口发现；国家页另观察到
`phoenixShopSearchPropertiesByLocation` 返回200，酒店明细字段及分页随后已接入。
两者均不能当作已可用的全年价格接口；独立 HTTP 调用条件未验证。
公开官方合作 API 资料另见 [批量接口研究](../OFFICIAL_API_RESEARCH_20261009.md)。

下文为较早的观察记录，访问状态以每次实测的站点、浏览器和时间为界。

观察日期：2026-09-28，开发机 Chrome。选择官网首页的酒店建议「New York Marriott Marquis」，
在日期选择器选择 2026-10-07 至 2026-10-08、1 房 2 成人后点击 Find Hotels。
搜索结果页实际显示 `1 - 40 of 231 Results`；酒店卡片显示 `NYCMQ` 酒店每晚 `1,197 USD`（含 45 USD Destination Fee）。
随后点击该卡片的 View Rates，房型页显示所选入住和退房日期，并触发房型报价请求。
这是一次有界、正常的网页交互；没有使用验证码、动态 Cookie 或登录信息构造独立请求。
浏览器页面显示个人账户的 Sign In 文案，不能据此认定响应在匿名会话下相同。

## 已观察的官网请求

| 用途 | Method / URL | 日期 | 结果 |
| --- | --- | --- | --- |
| 附近酒店及最低列表价 | `POST https://www.marriott.com/mi/query/phoenixShopDatedSearchByGeoQuery` | `search.options.startDate/endDate` | HTTP 200；结果含酒店信息和 lowestAvailableRates；仅是列表起价，房型/Rate Plan 不完整 |
| 酒店详情 | `POST https://www.marriott.com/mi/query/PhoenixBookDTTHotelHeaderData` | 无 | HTTP 200；`variables.propertyId="NYCMQ"` |
| 房型报价 | `POST https://www.marriott.com/mi/query/PhoenixBookDTTSearchProductsByProperty` | `variables.search.options.startDate/endDate` | HTTP 200；`total=55`、`edges` 共55；房型、Rate Plan、会员状态、基础价和含税总价可读 |

房型请求 Content-Type `application/json`，`operationName` 与 GraphQL `query` 是浏览器实际发出的内容；
`apollographql-client-name=phoenix_book`、`apollographql-client-version=1`、
`application-name=book`、`graphql-operation-name`、`graphql-operation-signature` 与
`graphql-require-safelisting=true` 是观察到的非机密头。两个 GraphQL 文本逐字保存在
`backend/app/providers/queries/marriott_*.graphql`，签名在 `backend/app/providers/marriott.py`。
没有保存 Cookie、Authorization、浏览器会话标识或用户 ID；也尚未证明
这两个 operation 无须已有浏览器会话即可访问。

房型请求的 `search` 输入为 `propertyId=NYCMQ`，`options` 包含
`startDate=2026-10-07`、`endDate=2026-10-08`、`quantity=1`、`numberInParty=2`、
`childAges=[]`、`productRoomType=[ALL]`、`productStatusType=[AVAILABLE]`，
`rateRequestTypes=STANDARD/PREPAY/PACKAGES/CLUSTER(MRM)`，
`isErsProperty=false`、`disabilityRequest=ACCESSIBLE_AND_NON_ACCESSIBLE`；`limit=150, offset=0`。
这些参数来自实际请求，不是推测。当前 55 条可一次返回；超过 150 条时会拒绝不完整结果，
后续需验证分页再放开。

## 映射和实际样本

`catalog.propertyById` 映射 `id`、`basicInformation.name/brand/currency/latitude/longitude`、
`contactInformation.address`、`seoNickname`。仅支持按已知五位酒店代码查找详情；
尚未实现城市或全球批量发现。酒店名/城市可通过本地数据库 API 检索。

房型节点 `basicInformation.type` 是房型代码，`name+description` 是展示房型，
`ratePlan[0].ratePlanCode` 是 Rate Plan，`rates.name` 是价格名称，
`basicInformation.isMembersOnly` 是会员价标志。
`totalPricing.rateModes.subtotalPerQuantity.amount` 是此例 1 房 1 晚基础价；
`grandTotal.amount` 是含税费总价。`MonetaryAmount.amount` 必须按 `decimalPoint` 缩放。
实际第一条公开响应：`115200 / 10² = USD 1152.00`，
`137706 / 10² = USD 1377.06`，强制费用单独为 USD 45.00。
房型 `dbdb`、计划 `AP0K`、会员价、预付不可取消描述。
总价与基础价差额同时含税及费用，故 `tax=NULL`；没有将差额误标为税。
积分、早餐和退款字段目前也填 NULL，不从名称猜测。
响应的公开产品 ID 编码含 `NYCMQ|AP0K|DBDB|2026-10-07|2026-10-08|...`，
解析时会核对酒店、计划、房型、日期，避免错配。精简的脱敏响应样本在
`backend/tests/fixtures/marriott_observed.json`；其中仅投影 55 条中的第一条，
`total` 改为 1 供单条解析测试，不代表原始响应的总数。

## 访问状态及边界

2026-09-28 浏览器采集试验：一个未登录的交互式浏览器会话从官网公开搜索页选择 NYCMQ，
2026-09-28 至 09-29、1 房 1 成人的搜索列表显示 USD 652/晚；进入房型页并开启
“Show with taxes and fees”，Flexible Rate 的会员价显示 USD 751/晚、非会员价
显示 USD 765/晚。另验证官网页面中的 `availabilityCalendar.mi?propertyCode=NYCMQ`
可按酒店代码打开预订页，网页日期控件能选择 2026-10-07 至 10-08，住客数能改为2。
新 `MarriottBrowserProvider` 实验代码先从首页输入酒店名称、选中酒店建议，
再从搜索结果中点击对应酒店的 View Rates；进入房型页后通过 Chromium 点击日期和住客控件，
提取非会员含税费价，
不把列表起价、会员价或缺失价格写入报价。交互式浏览器可能保有之前访问留下的站点状态；
实验适配器不复用该浏览器的会话。
浏览器版需要同时提供已知五位酒店代码和酒店名称，房型页再次核对代码与入住/退房日期；
不从搜索结果第一项猜测酒店，也不再直接拼接 `availabilityCalendar.mi` 地址。
本地实测新的 Playwright headless Chrome 会话打开官网首页返回 HTTP 403 / Access Denied；
普通窗口模式首页曾返回 200，但 30 秒内未出现可用的搜索控件。
经 `http://192.168.50.4:7890` 代理的独立请求及旧实验浏览器流程均收到万豪 Akamai 403；
修正后的完整页面流程尚未获得真实报价，因此仍不能宣称自动抓价已可用。
2026-09-29 重新实测修正后脚本：经 Nikki 代理的无头 Chrome 在首页返回 HTTP 403；
经代理的普通窗口最终显示 Access Denied。不走代理的无头 Chrome 也在首页返回 HTTP 403，
因此不能将失败单独归因于 Nikki。用户手动浏览器可正常搜索价格，说明受限的是当前新建
自动化浏览器会话；尚不能区分浏览器会话状态、自动化特征或其他服务端策略。

2026-09-30 复测：同一代理下新建无头和有头 Chrome 会话打开官网首页均为 HTTP 403 / Access Denied；
直接打开 NYCMQ 酒店详情页也为 HTTP 403。排查发现生产 Worker 此前仍连接独立 HTTP 适配器，
与“操作官网页面”的要求不符，现已切换到浏览器适配器，并从任务酒店记录传入酒店名称。
这只修正抓取路径，不代表新浏览器会话已获准访问或已经取得真实报价。
同一代理下改用安装版 Edge 的全新自动浏览器会话打开官网首页仍为 403 / Access Denied，
因此仅更换浏览器通道没有恢复页面访问。

同一开发机浏览器正常显示 55 条房型报价。独立 Python httpx 请求未成功：
官网首页直接请求返回 403，报价 POST 连接失败。尚未证明这份请求模板在
Synology/OpenWrt Nikki 网络下可独立长期运行，也没有伪造成功状态。
Provider 默认关闭；用户开启后，403 归类 `BLOCKED_BY_ANTIBOT` 并暂停 6 小时，
429 至少暂停 1 小时，无反爬绕过。浏览器成功仅证明结构和字段，不能代替 NAS 实采验收。

当前适配只接受 1 房 1 晚公开现金报价；积分及 Cash+Points、多房、多晚、不同币种
和不完整分页会拒绝或不生成数据。健康检查使用 NYCMQ 的未来单晚，
状态必须由实际成功请求和有效解析决定。`phoenixShopAdvSearchInventoryDate`
仅返回可预订日期上限，不能当报价接口。

下一步：在 NAS 上经当前网络做一次低频、正常请求测试；若 403/挑战则保持 BLOCKED。
若正常成功，复核不同国家、币种、房型和远期日期的官网页面，再考虑默认自动化。

## 2026-10-01 对照复测及代码修正

普通 Chrome 从 `default.mi` 输入 New York Marriott Marquis、选中建议并搜索，
2026-10-01 至 10-02、1 房 1 成人的页面实际显示 222 个结果，NYCMQ 列表起价 USD 632/晚。
这仅是列表价，未写入报价或发送提醒。独立 Chrome 浏览器脚本使用
`http://192.168.50.4:7890` 在同一首页收到 HTTP 403，标题 Access Denied；
失败发生在选择酒店/日期之前。不能将普通浏览器可用推断为 Worker 可用，
也不能将脚本失败单独归因于用户网络。会话状态和浏览器运行环境的具体影响仍未确定。

代码检查另发现并修正：房型最多读 8 个的截断、房型缺失被当成成功空结果。
现读取全部已显示房型，结果未加载或公开含税报价不可验证时报告明确错误，
不把它写成成功“无房”。一次性测试关闭共享浏览器池，消除结束时的事件循环清理异常。
相关新增测试及全套 117 项本地回归通过；随后官网实测仍为 HTTP 403，尚未解决访问问题。
# 2026-10-01 官网操作复核

普通 Chrome 从官网首页搜索 New York Marriott Marquis，进入房型页后选择
2026-10-20 / 10-21、1 房 2 成人，并开启含税费展示。
DBDB 双床房公开非会员价包括 USD 936、1016、1092/晚；没有点击 Select 或提交订单。
这只是普通浏览器页面证据，不是独立 Worker 成功。

发现并修正两处真实控件问题：`Stay Dates` 大小写与原脚本正则不匹配；
`View Rates` 外层按钮点击不展开且页面脚本报错，点击其可见内部文字 span 正常展开。
适配器现在忽略日期按钮大小写，等待税费切换后房卡重新渲染，再点击可见文字。
同日独立普通 Chrome 采集会话，无论是否显式设置代理，首页仍返回 Access Denied。
因此当前不能宣称万豪自动采价已通过，也不能将失败归因于用户无法访问官网。

## 全球公开目录（本地未发布实现）

2026-10-01 普通 Chrome 通过首页 Book → Hotel Locations 到达
`https://www.marriott.com/hotel-search.mi`，页面 Property Directory 显示 10,423 Properties。
逐一展开实际九个字母组 A-C、D-F、G-I、J-L、M-O、P-R、S-U、V-X、Y-Z，
分别读到 398、283、187、210、302、175、312、124、29 个目的地链接，去重共 2,013 个。
这是目的地数量，不是已经收录或查价的酒店数量；未尝试此前被浏览器工具拦截的 XML 地图。

正常点击 A Coruña 目的地，实际 Showing 1-2 of 2 Hotels，主酒店卡片为 LCGCO
AC Hotel A Coruna 和 SCQAK Hotel Palacio del Carmen, Autograph Collection。
后者是邻近城市酒店，因此不把目的地标题当成酒店城市。目录卡片起价的可见日期
与链接日期不一致，目录解析器只读取酒店身份，绝不产生价格记录或 Telegram 提醒。

正常点击 Abu Dhabi，公开别名重定向至
`/en-us/destinations/united-arab-emirates/abu-dhabi.mi`。
第一页实际 Showing 1-12 of 14 Hotels；点击页面 Next，实际 `?pg=2` 显示 13-14 of 14，
仅两个主酒店卡片 AUHWH（W Abu Dhabi - Yas Island）、AUHLC（Al Wathba, a Luxury
Collection Desert Resort & Spa, Abu Dhabi）。最后一页 Next 已禁用，未猜测下一页地址。

已加入 rendered public DOM 解析和持久化 Worker 目录任务：九组必须读完，主卡片数量
必须与当前页范围相符，目的地与页码核对，后续仅沿已观察到的公开 Next 链接派发。
HotelData 去重入库后参与一年日期轮询；跨目的地相同酒店不重复算全局数量。
每页完整读取与全球完整覆盖分别记录，完整页面按正常目录周期再查，不因分页
本身无法证明全球总量而每小时重复打开。

独立普通窗口浏览器经已配置代理探测同一公开目录，本次主文档 HTTP 200，
但标题和正文为 Access Denied，零可用控件。不能把 200 当成目录成功，也不能
将本次响应写成 403。普通浏览器上述目录观察不代表自主 Worker、Linux/NAS 已完成。

新增全局目录审计保存每页公开子链接和酒店代码，沿当前根目录可达图汇总，
排除已脱离当前目录的旧页面，分页和城市重叠酒店按代码去重。失败、过期、
缺少审计信息或读取不完整的页面都保留缺口。只有所有当前页面有效读取且去重酒店数
与官网全球总数相同，才可标记 FULL_CATALOG_VERIFIED；该标记仍不代表全年报价完成。
旧版成功记录缺少每页审计信息时只重新核对一次，不据旧累计 URL 推断全球成功。

## 2026-10-05 持久目录低频复核（本地，未发布）

距旧失败数日后，经原有代理、普通专用Chrome和生产Worker再次读取正常Hotel Locations
入口，未访问此前被浏览器工具拦截的XML。任务6f4bd1c8-21dd-4a55-859e-cf0dc32419ed
返回`FAILED / BLOCKED_BY_ANTIBOT / Marriott challenge page`，零酒店、零报价、零通知。
错误文本未记录本次HTTP状态，不假定403。共享暂停至2026-10-05 08:29:16（Asia/Shanghai）。
`audit_ihg_catalog.py --provider marriott`现保存目录游标、集团状态与失败到专用验收库
`work/marriott-directory-audit-20261005/catalog-audit.sqlite`，下次不会创建新库重置冷却。
没有复制普通用户会话、改变代理/通道或在暂停内重试。自动目录和报价仍未通过。
