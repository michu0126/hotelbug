# GHA DISCOVERY 研究记录
## 2026-09-29 官网页面实测（当前实现方向）

不再以寻找私有接口为前提。安装版 Chrome 通过配置的 HTTP 代理，直接读取详情页和预订页，均返回 200。
Avani Sukhumvit Bangkok Hotel 的详情页 BOOK NOW 链接提供公共预订参数 `hotelId=10624`、`startDate`、`endDate`、`room1Adults`、`room1Children`。
把日期改为 2026-10-07/08 后，预订页实际显示 `Wed, 07 Oct - Thu, 08 Oct 2026`、`ROOM 1: 2 ADULTS`，三种房型有报价。
点击包含 checkbox 的可见 label 切换含税价格（直接 check 隐藏 input 会被 label 遮挡）。DELUXE KING 的非会员起价从 THB 5,900 变成含税 THB 6,944。
点击 VIEW RATES 后，可见具体计划 Avani Flexi、THB 6,944/night、Including taxes and fees；另外两个 DISCOVERY 会员计划为 THB 6,250 和 6,674，不应混入非会员基线。
DOM 中 `.tid-selectBtn` 的三层父节点包含计划标题 h5、价格 h5、税费文字、`.tid-viewTotalStay` 条款按钮；移动端重复节点需要去重。
`backend/tests/probe_gha_booking.py` 可重现实测，只查价，不选择房间、不提交预订。
已实现 `GHABrowserProvider` 并注册：读取每个房型的明确 NON-MEMBER RATES 含税金额，再打开具体计划，仅保存与该金额/币种匹配的非 DISCOVERY 计划。房型和计划用名称哈希标识，不伪造官网代码。按网页展示保存 nightly 价格，税额、取消和早餐字段未知时为 NULL。
真实 Worker 测试 `tests.smoke_accor_pipeline --provider gha --proxy ...` 已通过：发现任务 SUCCEEDED、查价任务 SUCCEEDED，3 条 PriceHistory，Provider ONLINE。此轮官网显示 USD 207/217/252，币种按实际页面保存而非固定 THB。
11 个离线测试覆盖日期、人数、酒店 URL、会员排除、税费、币种和金额匹配。仍需验证其他品牌、跨年日期的页面格式、精确总价/条款及全量目录，不能据单酒店样本声明 GHA 全量完成。

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
