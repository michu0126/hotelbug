# IHG 研究记录
状态：未实现；Phase5。官网入口：https://www.ihg.com/。
2026-09-27开发机：robots.txt 200；旧redirect预订页NYCHA返回403 Access Denied，未获有效报价JSON。
数据路线：优先从正常官网搜索、日期和预订页面交互读取可见报价；当前全新浏览器会话无法走通，不把目录或 HTTP 200 当成报价。
价格Endpoint、Method、Payload、Headers、Cookie/Token、所有价格字段映射：未验证。
旧redirect参数含品牌/地区/日期，不能将目录或HTTP200作为酒店价格正确性的依据。
robots规则应在实现时复核，优先低频小样本验证。
2026-09-30 经配置代理，新建 Chrome 打开 Holiday Inn Express London - City 的官网 rooms 页面返回 403 Access Denied；sitemap-index.xml 与 robots.txt 独立请求也返回 403。用户正常浏览器可用与本测试会话结果不同，不能归咎于用户网络，也尚未取得价格。
同一页面改用安装版 Edge 的全新自动会话仍返回 403 Access Denied，仅换浏览器通道无效。
