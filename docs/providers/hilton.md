# Hilton 研究记录
状态：未实现；Phase5。官网入口：https://www.hilton.com/。
2026-09-27：官网英文酒店sitemap返回200；MLEHICI日期预订页返回403 Hilton Page Reference Code。
路线：正常搜索交互→实际网络请求→同酒店同日期现金/积分报价fixture→字段映射；受阻标BLOCKED。
不能基于其他项目示例猜测GraphQL Endpoint。
Endpoint/Method/Payload/Headers/Cookie/Token/价格税费及积分字段均未验证。
目录可读与报价可读分别记录；积分和现金分开保存。
