# 六大集团官网价格接口：实测与证据

调查日期：2026-10-23，Asia/Shanghai。已停止早早私有后台方向，本轮只研究各集团官网。

## 结论

确认了五个集团的明确价格端点。**只有 GHA 的本次样本完成了不带浏览器 Cookie、用户 Token 或应用密钥的独立 HTTP 实采；不是六集团已打通。**

| 集团 | 已确认端点 | 本轮结果 |
| --- | --- | --- |
| 万豪 | `POST https://www.marriott.com/mi/query/PhoenixBookDTTSearchProductsByProperty` | 历史官网真实响应/既有适配器；本次独立 HTTP 403，停止，无备用入口重试 |
| IHG | `POST https://apis.ihg.com/availability/v3/hotels/offers` | 本次官网浏览器 HTTP 200、匹配日期的 JSON 205 个方案；最小独立 HTTP 403 |
| 希尔顿 | `POST https://www.hilton.com/graphql/customer` | 历史官网 HAR 的 `hotel_shopAvailOptions_shopPropAvail`；本次首页 200 后 403，未重新查价 |
| 凯悦 | 尚未取得确切 JSON 价格端点 | 本次正常首页 HTTP 429，停止。`shop/rooms` 是页面导航，不冒充接口 |
| GHA | `POST https://oscp.ghadiscovery.com/api/v3/booking/hotel/rooms/rates` | 本次独立匿名 HTTP 200，7 房型；另有页面对应 JSON 20 个公开/会员方案 |
| 雅高 | `POST https://api.accor.com/bff/v1/graphql` | 本次 `HotelPageHot` HTTP 200，8 个 offer；另一次正常表单已读到8条公开含税价，独立 HTTP 未验证 |

“浏览器请求成功”“保存响应后离线解析成功”“独立 HTTP 成功”“生产容器稳定运行”是四种不同结论。本轮没有更新群晖、生产注册表、调度、暂停状态或通知设置。

## IHG：实际请求和价格口径

官网 Hotel Indigo London - 1 Leicester Square (`LONLS`)，2026-11-10 → 11-11，1房2成人。新空白 Chrome 正常选择日期及人数，进入 select-roomrate 页面，自然发出真实价格请求。

- 路径 query 的 `fieldset` 包括 `rateDetails`、`rateDetails.policies`、`rateDetails.bonusRates`、`rateDetails.upsells`、`rateDetails.packagesAdditionalInfo`。
- 请求的 `startDate/endDate`、`hotelMnemonics` 和 `products[].startDate/endDate/quantity/guestCounts` 必须一起匹配。
- `guestCounts` 的 `AQC10` 为本次成人数；不把请求前页面自动加载的今天/1成人响应混入目标日期。
- `rates.ratePlanCodes` 必须取自实际官网请求，不根据名称随意生成。样本有 IVAN1/3/5/6/7/IVANI。
- 响应 `hotels[].hotelMnemonic`、根日期、主产品 period、数量和人数均验证；酒店币种 `propertyCurrency=GBP`。
- `rateDetails.offers[]` 对应真实方案，关联 `productDefinitions` 的房型和 `ratePlanDefinitions` 的方案。
- `amountBeforeTax` 是税前金额；`amountAfterTax` 是含税金额；`baseAmount` 在该样本可能已经含税，不能当税前价。
- `feeTaxTotal` 仅当所有 subtotal 明确为 TAX 才写 tax，否则保持未知；不把杂费差额全部当税。
- `isLoyaltyIdRequired` 是会员要求的显式字段；保留 package/voucher/ID/group/tentative 等限制，不根据文字猜测。
- `policies.isRefundable`、`cancellationNoShow.description/deadline.dateTime`、`guarantee.type` 可读取。取消截止是酒店当地时间，但本次没有可靠时区，不计算 `free_cancel_now`。

对应正确条件的完整 JSON 离线解析 **205 条：79个非会员标记、126个会员标记，GBP，最低含税316.22**。这是该请求/响应的字段解析计数，不是全球覆盖，也不是205个独立页面逐项复核。

原页面适配器后来在 UI 校验阶段失败，浏览器任务整体为 `FLOW_INCOMPLETE`。这不改变已经捕获的正确日期/人数 HTTP200 JSON，但不能把整体任务写成页面验收成功。

最小独立 HTTP 对照改用11-12→13，显式只有 Accept/Content-Type/X-IHG-API-KEY，无用户会话，返回403。立即停止，没有增加头/切代理/换入口重试。无法仅由403断定缺的是哪一个头或 Cookie。

新增实验模块：`backend/app/providers/ihg_api.py`。保存真实响应的两条原样投影作为 `backend/tests/fixtures/ihg_api_observed.json`，并标注原始205条与投影2条，不能称投影就是全部报价。

## GHA：独立 HTTP 已成功的范围

Avani Sukhumvit Bangkok Hotel：官网 ID `10624`，实际预订系统 ID `8188`，hotelCode `AVVSUK`，brandCode `AV`，chainId `18336`。两个 hotelId 不能互换。

- 11-09→10 的官网正常浏览器请求：2房型、20方案（10公开/10会员），完整 JSON 可由新解析器读取。
- 11-10→11 的独立 httpx 请求：HTTP200，7房型，44,665字节 JSON。没有浏览器 Cookie、Authorization 或 app key；仅显式指定 JSON 内容类型（HTTP库仍有普通默认头）。这次只保存结构/状态，未保存完整正文，不能声称这份正文已逐字段验收。
- payload 带 `numberOfRooms/Adults/Children`、`childAges`、`startDate/endDate`、实际 hotel/brand/chain 映射、content/full 和渠道参数。
- 公开/会员必须按 `memberRate` 布尔字段分开。
- `amountWithTaxesFees` 是样本明确含税费总额；`taxInclusive=false` 时 `amount` 为原价。不要将 `dailyPrice` 误当税前价。
- 税费明细、早餐和取消政策在这份 rooms/rates JSON 中不完整，保持未知。
- 响应不可靠地 echo 酒店/日期/人数，必须绑定同一次实际 request/response，不能把任意裸 JSON 当已验证报价。
- 官网 CMS/页面提供 `reservationsEngineCode`、`synxisHotelId`、`synxisChainId` 等映射，不是所有 GHA 酒店都使用同一预订引擎。

新增 `backend/app/providers/gha_api.py`：显式身份模型、请求构造、JSON解析、单次HTTP客户端、同实例并发1和拒绝暂停。**未注册到生产**；正式调度前还需真实映射发现、持久品牌暂停及适用引擎范围控制。

## 雅高：官网 GraphQL 不是所有响应都有价格

实际价格 operation 是 `HotelPageHot`；HotelPageCold/HotelMap/HotelsNearbyCold 是其他用途，不能因为都是 GraphQL 就当房价。

已观察变量：`dateIn/dateOut`、`hotelOffersHotelId`、`nbAdults`、`childrenAges`、`countryMarket`、`currency`、`offersSelectionFilters`、`use=NIGHT`、`hideMemberRate=false`、`totalRoomInBasket=1` 等。

- 11-06→07，官网0338正常表单成功读到8条公开含税 EUR 价。
- 为补齐首次未保存的 GraphQL 正文，改用11-08→09；取得8个 `data.hotelOffers.offersSelection.offers[]` 的真实 JSON，随后旧DOM币种等待超时，不把第二轮整体任务称成功。
- `pricing.aggregationType=TOTAL_STAY`，币种在 `pricing.currency`；本次税费文案明确 included。
- **`pricing.main.categories=[MEMBER_RATE]`，不是公开价。`pricing.alternative.categories=[STANDARD]` 才是公开选项，并有自己的 offerId/bookable 字段。**
- 房型 `product.id`、方案 `rate.id/legacyId`、`dateIn`、lengthOfStay、occupancy 要关联校验。
- main 的 `simplifiedPolicies` 有取消和担保信息，但 alternative 没有自身政策，不应直接继承 main 的取消规则。
- 空白浏览器会话也由官网生成 identification-token；这不是需要酒店账号登录的证据。最小头及独立 HTTP 尚未验证，不发布这些头值。

## 万豪 / 希尔顿 / 凯悦边界

万豪已存在实际观察的 GraphQL 查询和解析器，可分基础价/含税总价、房型、方案、会员标记，中文站真实响应也有既有脱敏样本。本次 XIYWH/11-09→10 独立单次调用403后停止；未切换域名或会话绕拒绝。

希尔顿历史 HAR 证明确切 operation，已有 `collector/adapters/hilton_samples.py`、`hilton_rates.py` 处理房卡起价和选定房型方案。房卡 `moreRatesFromRate` 与完整 `roomOnlyRates/packageRates` 不等价；nested Honors 不能当公开价。本次首页被拒，没有新报价。历史45项测试通过不等于实时采价成功。

凯悦本次首页429，无法取得实际价格请求。现有中文DOM适配器并不证明有某个特定 JSON API；这项仍未解决，不编造路径、签名或访问条件。

## REA 与本地证据

REA负责静态/留存数据分析；正常浏览器动作由辅助 Playwright 完成，独立请求由辅助 HTTP 工具完成。两者不可冒充同一种证据。

- IHG派生HAR投影：`ev_7311b9b75bba4d3ecc9920cc606269faff5bc4fed36d7deb9a1f4b1077e6b237`。原始响应SHA `547f8ebf61de15b868edd5134d9985a55a1744df39afb5143bd21fc859b732d8`。非原始浏览器HAR，投影边界/原文件哈希见 manifest。
- GHA真实公共JS静态分析：`ev_9702cada2e6e7867dbaaaf307e34e49c85bc42eb9cfd5ee4396c7af534dc6bd0`，选择3个JS，0解析失败，其他库存文件/动态依赖仍未知。
- Accor真实公共JS静态分析：`ev_3b633da30f49d3b46ab1400349c4ef8d63c611a25dcdd64600760a2a72914100`，选择7个JS，0解析失败，其他库存文件/动态依赖仍未知。
- Hilton历史HAR字段投影：`ev_7dc7632bb8581d2cb1c6e8a8af065157ea03db6d60b0f8d92bbbf3fbc27e303d`。不是当前实采。原 `www.hilton.com.har` SHA `bb3e661acc7db31eec20b9258576fb105c24a49d9b9492b16fd79d282330483d`、ordinal118；旧123.har已是其他流量，不能再以原ordinal117证明旧完整房价样本。

完整研究材料仅存于 `work/official-interfaces-20261023/`（已忽略，不发布原站JS/原始私人捕获）。未停止 Container Manager，未更改已有酒店、价格、Cookie、Telegram、阈值或代理配置。所有本轮自建浏览器已关闭，用户浏览器/微信未关闭。

## 接下来可实施的路径

本轮验证：新增 GHA/IHG 接口及原有相关页面回归 **107项通过**；完整后端回归 **2,259项通过**；新增模块 Ruff 通过。离线测试不替代上表中的实时网络验收。

1. GHA先接真实目录/CMS的预订映射，再按适用引擎走JSON；其他引擎保留原页面方式。
2. IHG和雅高优先监听官网自然发出的JSON，替代脆弱的价格DOM解析；当前不假装独立HTTP已通。
3. 万豪和希尔顿保留既有接口/样本，等待合法正常会话可访问后验证；凯悦必须取得真实网络样本再定接口。
4. 这些按住宿查询的接口不是已验证全年批量日历；仍需限速轮询、历史积累、同条件复查后再通知。
