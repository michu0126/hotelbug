# 官方 API 与批量采集核验（2026-10-09）

全球酒店不需要人工逐家录入。现有官网目录、sitemap 和持久游标可自动发现酒店；
本轮重新核对各集团官方 API 的批量能力和接入条件。以下区分公开文档、匿名访问
实测与项目已经启用的实现，不把文档中的能力视为已经取得的生产报价。

## 官方证据

| 集团 | 官方能力与接入条件 | 当前判断 |
| --- | --- | --- |
| 雅高 | [Domain Offers](https://developer.accor.com/api-portfolio/domain-offers/api-description-38) 的多酒店 best-offers 支持最多 300 家/请求；另有单酒店日历及完整报价。[使用说明](https://developer.accor.com/api-portfolio/domain-offers/how-use-it-36)要求 clientId，Authorization 取决于客户端授权。[开发平台首页](https://developer.accor.com/)提供免费注册和测试环境 API key。 | 批量/日历能力有官方文档；免费生产权限尚未证实，也未取得可用生产凭据。文档中的示例 clientId 不代表分配给本项目的权限。 |
| 希尔顿 | [官方示例](https://developer.hilton.io/sample-app/sample-code/check-available-rooms/)的 Multi Property Shop 最多 30 家/请求，返回各酒店各计划的最低产品，完整房型需单酒店查询。[API 文档](https://developer.hilton.io/api-catalog/hospitality-partner/distribution/directconnectshopping/directconnect-shopmultipleproperties/)面向 Direct Connect partners。[鉴权](https://developer.hilton.io/sample-app/authentication/)要求 Hilton 分配 Client ID/Secret 和 OAuth Token；[目录说明](https://developer.hilton.io/api-catalog/hospitality-partner/)按合作协议逐接口授权。 | 文档公开，生产调用需要正式权限；尚未取得凭据或验证批量生产报价。 |
| IHG | [PartnerConnect](https://partnerconnect.ihg.com/)明确提供 RoomService 的实时酒店价格、可售情况及酒店内容 API。 | 已确认官方合作接口存在；公开首页未给出匿名调用契约或批量上限，不能推断立即免费可用。 |
| 万豪 | [开发入口](https://devportalprod.marriott.com/)可访问，但本次[API 列表](https://devportalprod.marriott.com/apis)只显示空列表；[CPP](https://cpp.marriott.com/s/login/)的官方搜索索引说明其为合作伙伴登录平台。项目另有真实官网 Phoenix GraphQL 观察记录。 | 不能据开发门户或第三方示例推断存在自助匿名报价 API；独立匿名实测被拒绝，见下。 |
| 凯悦 | 本轮官方域名检索未找到可验证的公开自助现金报价 API 文档；这不证明 API 不存在。[全球酒店列表](https://www.hyatt.com/zh-CN/explore-hotels/list)是现有自动目录来源。 | 继续使用官方目录及已观察的正常网页报价流程；批量搜索/日历响应待实测。 |
| GHA | 本轮官方域名检索未找到可验证的公开匿名报价 API 文档；[合作入口](https://www.globalhotelalliance.com/become-a-partner)没有公布报价 API 契约。 | 现有 sitemap 自动发现和酒店页面 BOOK NOW 标识可用于目录扩展；批量报价接口待实测。 |

## 本轮独立请求结果

- 2026-10-08 13:32 UTC：雅高官方 catalog 的匿名 GET
  `https://api.accor.com/catalog/v1/hotels?q=paris&fields=statistics.hotel.total,results.hotel.id,results.hotel.name`
  返回 HTTP 401 / `AuthRequired`。这是目录接口结果，不能外推为所有雅高接口永久不可用。
- 2026-10-08 18:29 UTC：通过现有 `MarriottProvider.get_hotel_details("NYCMQ")`
  独立匿名调用已观察的官网酒店资料 operation，返回
  `BLOCKED_BY_ANTIBOT` / `Access refused`；随后没有继续发送报价或其他查询。
- 上述请求均未证明匿名批量价格可用；本记录不保存 Cookie、密钥、用户会话或原始响应。

## 现有实现与下一步

2026-10-09后续实测新增：万豪中文全球目录页面正常请求
`POST /mi/query/phoenixShopSearchPropertiesByLocationInput`，
`search.options.countsOnly=true`，所有 facet dimensions 为真正的空数组。
HTTP200响应含151个国家／地区分组、明确计数和全球总数，没有酒店明细或日期价格。
生产 Browser Worker 已消费这个正常页面的响应并保存151个国家入口；成功本次总数
10,437（较早捕获为10,435），这只是官网报告数，不是已收录酒店/报价覆盖数。

2026-10-10补充：正常国家页的 `phoenixShopSearchPropertiesByLocation` 请求
使用 limit40、offset0；正常页码点击返回 offset40 / nextOffset80。生产目录已解析
这些页面自身返回的完整 edges，不独立重放请求或复制用户会话。中国前两页均
40/40；这是酒店身份目录，不是含入住日期的批量报价接口。同一目录验收库自动
生成的 PBHPR 单晚任务另由官网正常房型页面取得16条真实公开含税报价并入库。
同一持久库随后自动读取不丹列表，保存2家真实酒店并生成日期任务；国家列表请求为
`phoenixShopSearchPropertiesByLocation`，返回200，明细与分页于10月10日接入。
未复制用户 Cookie、请求鉴权头或重放用户会话；也尚未证明匿名独立 HTTP 可调用。
详见[万豪最新记录](providers/marriott.md)。

当前 `backend/app/providers/registry.py` 的六集团 Worker 工厂全部使用浏览器适配器。
`backend/app/providers/marriott.py` 保留实验 HTTP 实现，尚未替换生产工厂。
`docs/providers/marriott.md` 记录已在官网正常操作中观察到的多酒店查询
`phoenixShopDatedSearchByGeoQuery`、酒店资料和房型计划 operation；浏览器观察成功
与本项目独立 HTTP 成功分别核验。

批量监控应按以下顺序接入现有调度：

1. 自动遍历官方全球目录，按集团酒店代码去重，保存分页和目录游标。
2. 对已证实可用的多酒店或日历查询按其实际批量限制分片，覆盖滚动一年内日期。
3. 将列表/日历最低价保存为筛选依据；对大幅降价的酒店日期读取具体房型、计划、
   入住人数、币种和税费依据，核对同条件历史记录后触发 Telegram。
4. 为每个集团分别显示目录覆盖率、日期覆盖率、最新成功时间和实际采集方式；
   未开放日期和查询失败不当作零价格，个别成功样本不代表全球覆盖完成。

本轮只核验能力与接入条件；没有宣称六集团批量生产报价或全球一年监控已完成。
