# 希尔顿报价样本核对（2026-10-18）

历史报告：这是上一轮房型起价样本的结论。用户后来已覆盖 `123.har` 并提供完整方案页面，
最新解析与自动网页适配进展见 [具体方案核对报告](HILTON_FULL_RATES_REVIEW_20261018.md)。

## 结论

用户提供的国际站 HAR 已包含**有日期、有房型、有币种的官网房型起价**，不是中国站酒店目录的无日期 `minPrice`。
已完成本地离线解析，未重放请求、未连接官网、未导入 Cookie、未写正式历史库、未通知或部署 NAS。
这些数据目前仍属于房型卡片最低展示价，不是已核实全部条件的公开房价方案。

## 证据范围

| 文件 | 请求数 | 房型搜索响应 | 原始房型记录 |
| --- | ---: | ---: | ---: |
| `www.hilton.com.har` | 145 | 1 | 13 |
| `123.har` | 177 | 2 | 26 |

两个文件的第一次查询是**同一原始请求被导出两次**（请求开始时间相同），13 条记录去重后总共 26 条。
不能将重复导出的 13 条当作独立抓取、历史基线或二次确认。

已观察的页面：`/zh-hans/book/reservation/rooms/`。
已观察的响应位置：`/graphql/customer`，操作名 `hotel_shopAvailOptions_shopPropAvail`。
只记录官网正常页面产生的响应，不实现独立请求重放，也不复制请求头、Cookie、身份或缓存标识。

## 已确认字段与价格

酒店响应代码为 `SHAQTWA`（前滩华尔道夫）；本次请求代码为 `SHAQT`。
这是已观察的单个别名映射，代码不将任意五位酒店前缀都视为同一家酒店。
两次均为 **1 间房、1 成人、0 儿童、住 1 晚**，不是默认配置里的 2 成人。
币种由 `shopAvail.currencyCode = CNY` 明确给出，不从 `¥` 猜测。

以大床豪华客房 `K1D` 为例：

| 入住 → 离店 | `rateAmount` | 向上取整的显示价 | `fullAmountAfterTax` |
| --- | ---: | ---: | ---: |
| 2026-11-11 → 11-12 | 2391.54 | ¥2,392 | ¥2,535.03 |
| 2026-11-17 → 11-18 | 2500.10 | ¥2,501 | ¥2,650.11 |

- `roomTypeCode` / `roomTypeName`：房型身份与名称。
- `moreRatesFromRate.ratePlanCode`：最低展示价引用的方案代码，本次为 `HPPAP4`；仅有代码不足以确认其完整方案条件。
- `rateAmountFmt(decimal: 0, strategy: ceiling)`：查询明确要求向上取整，不适合用作精确历史值。
- `amountAfterTaxFmt(decimal: 0, strategy: ceiling)`：同样是取整的含税展示文本。
- `fullAmountAfterTax: amountAfterTaxFmt`：它是查询别名对应的较精确含税格式化文本，**不是独立的结算最终总额字段**。
- `ratePlan.serviceChargesAndTaxesIncluded = false`：不能把 `rateAmount` 直接标为全部含税费。
- `roomOccupancy = 3`：房间最大可住人数，不是本次搜索成人数；本次人数取请求的 `numAdults = 1`。

## 为什么尚未进入降价通知

本次房价在 `moreRatesFromRate`（房型卡片“更多房价起价”）中；`quickBookRate`、`bookNowRate` 为 null。
完整房价方案名称、取消／付款条款没有出现在该卡片价格对象内，`hhonorsMembershipRequired` 也未返回。
`confidentialRates = false` 不等于已经证明不要求会员；代码不会由 `HPPAP4` 字面推断方案类型。

因此新解析器输出 `HiltonRoomObservation`，固定 `eligible_for_alerts = false`，与可比较 `Quote` 分开。
数据不会误写检测历史或触发 Bark。下一步核对任一房型展开后的具体房价方案页面，补齐方案身份及会员条件，
之后才接入正式网页采价、同条件历史比较与独立二次确认。

## 可复现离线分析

在 collector 目录运行：

```powershell
..\work\collector-venv\Scripts\python.exe -X utf8 -m scripts.analyze_hilton_har E:\Codexspace\www.hilton.com.har E:\Codexspace\123.har
```

输出为 `data/hilton-har-analysis.json`（被 Git 忽略），只含价格／日期／条件白名单及原始文件名、条目序号。
回归夹具只保留两个房型的必要公开字段，不包含账号、Cookie、请求凭证或原始 HAR。
