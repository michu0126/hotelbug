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
