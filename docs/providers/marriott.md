# Marriott 研究记录
状态：未实现；Phase 2 唯一首个生产 Provider。证据时间2026-09-27，开发机。
入口：https://www.marriott.com/，官网酒店页→选择日期→搜索。
旧版 availability.mi 携带 NYCMQ、2026-10-07/08 时页面200，但仍显示 Enter your dates，不能认定日期生效。
观察到 POST https://www.marriott.com/mi/query/phoenixShopAdvSearchInventoryDate。
Payload operationName=phoenixShopAdvSearchInventoryDate，variables={}；返回 advancedReservationDateLimit/singleDateLimit。
**这仅是最远预订日期，不是报价 API，禁止实现为 search_rates。**
房价 Endpoint / Headers / Cookie / Token：尚未确认。需要重新走实际日期搜索，保留脱敏字段样本。
hotel_id/name 可从目录/页面取得；room/rate/cash/tax/total/points 映射待确认。
目录 sitemap-index.xml 曾返回200，不代表报价可用。异常时返回标准化错误，不猜URL。
