# Marriott 官网研究记录（Phase 2 实验版）

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
