# GHA DISCOVERY 研究记录
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
