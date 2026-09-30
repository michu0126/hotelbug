# Hilton 研究记录
状态：未实现；Phase5。官网入口：https://www.hilton.com/。
2026-09-29 实测补充：通过配置的 HTTP 代理，安装版 Chrome 的全新自动浏览器能加载英文首页（HTTP 200）。首页存在 `input[name="query"]`，输入 Hilton London Paddington 后实际 DOM 的 assertive live region 显示 `No results found`，未出现 option，并非已证实的定位器错误，也不能据此认定整站拒绝访问。另一轮未等到搜索输入框；仅凭超时不足以分类反爬。测试脚本 `backend/tests/probe_hilton_flow.py` 保留各阶段状态和页面诊断。尚未获得真实房价；下一步验证 Cookie 提示异步出现及搜索输入时机，并比较城市搜索与酒店搜索。
2026-09-27：官网英文酒店sitemap返回200；MLEHICI日期预订页返回403 Hilton Page Reference Code。
路线：正常搜索交互→实际网络请求→同酒店同日期现金/积分报价fixture→字段映射；受阻标BLOCKED。
不能基于其他项目示例猜测GraphQL Endpoint。
2026-09-29 后续验证：城市 London 逐字输入后同样显示 No results found，但日历控件可打开，显示明确的入住/离店日期按钮。发现并修正探测脚本 Cookie 按钮定位错误：可访问名称为 `dismiss cookie message`，不是可见文本 `Do Not Accept`。修正后的新会话首页初始响应 200，随后出现 `Hilton Page Reference Code / SOMETHING WENT WRONG`，未到达有效报价结果。此轮不能证明 Cookie 修复解决了查价；也没有证据证明日期搜索提交成功。不连续重试同一失败会话。
Endpoint/Method/Payload/Headers/Cookie/Token/价格税费及积分字段均未验证。
目录可读与报价可读分别记录；积分和现金分开保存。
2026-09-30：英文 sitemap 索引 `/sitemap/en/sitemap-en.xml` 返回 200，包含 989 个子地图，其中 542 个路径含 `prop`；首个酒店子地图 `sitemap-en-prop-hp-001.xml` 返回 200、107 条 URL，包含酒店主页与 gallery/rooms 等子页。Hampton Aachen Tivoli 酒店主页浏览器返回 200，显示酒店名称和日期、人数、Check prices 控件；在下一次操作前页面转入 `Hilton Page Reference Code` 错误页，尚未拿到可比较报价。目录虽能用于发现候选酒店，不能证明查价功能已完成。探测脚本已扩展 `--property-url` 参数。
