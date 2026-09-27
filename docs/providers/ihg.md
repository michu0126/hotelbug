# IHG 研究记录
状态：未实现；Phase5。官网入口：https://www.ihg.com/。
2026-09-27开发机：robots.txt 200；旧redirect预订页NYCHA返回403 Access Denied，未获有效报价JSON。
数据路线：从正常官网搜索操作观察XHR/JSON；无法正常访问则BLOCKED_BY_ANTIBOT并停止，不绕过。
价格Endpoint、Method、Payload、Headers、Cookie/Token、所有价格字段映射：未验证。
旧redirect参数含品牌/地区/日期，不能将目录或HTTP200作为酒店价格正确性的依据。
robots规则应在实现时复核，优先低频小样本验证。
