# IHG 研究记录
状态：官网页面适配器已注册，单酒店开发机真实 Worker 任务已通过；全球目录、Linux/NAS 官网采价与长期稳定性尚未验收。官网入口：https://www.ihg.com/。
2026-09-27开发机：robots.txt 200；旧redirect预订页NYCHA返回403 Access Denied，未获有效报价JSON。
数据路线：优先从正常官网搜索、日期和预订页面交互读取可见报价；当前全新浏览器会话无法走通，不把目录或 HTTP 200 当成报价。
价格Endpoint、Method、Payload、Headers、Cookie/Token、所有价格字段映射：未验证。
旧redirect参数含品牌/地区/日期，不能将目录或HTTP200作为酒店价格正确性的依据。
robots规则应在实现时复核，优先低频小样本验证。
2026-09-30 经配置代理，新建 Chrome 打开 Holiday Inn Express London - City 的官网 rooms 页面返回 403 Access Denied；sitemap-index.xml 与 robots.txt 独立请求也返回 403。用户正常浏览器可用与本测试会话结果不同，不能归咎于用户网络，也尚未取得价格。
同一页面改用安装版 Edge 的全新自动会话仍返回 403 Access Denied，仅换浏览器通道无效。

## 2026-10-01 官网流程与独立任务实测

正常 Chrome 从伦敦目录进入 Hotel Indigo London - 1 Leicester Square（LONLS），
通过日期控件选择 2026-10-20 / 10-21，1 房 2 成人，点击 View prices。
读取展开后的房价方案，不使用目录起价。默认开启会员折扣，采集时逐房型用正常开关关闭。
普通浏览器公开价示例：OAAN 房型 GBP 368、394、398、409、424、439/晚。
房型代码来自 `ROOM_CODE…` / `room-card-title-…`，方案代码来自 Select 按钮的
`roomRate<room><plan>` 属性；没有点击最终 Select、登录或提交订单。

页面适配器修正了三处实际问题：整页加载事件在表单已可用时仍超时；
人数输入被其外层点击容器覆盖；官网展示现金价未进入日历/降价比较。
现在等待实际酒店/表单控件，点击 `roomAndGuestModal` 正常触发器，
提交前核对日期/人数，逐一展开所有房型并等待关闭会员折扣后的卡片更新。
预订页 URL 与显示的酒店、日期、人数再次验证后才接受报价。
URL 的 `qCiMy` / `qCoMy` 使用 **零基月份 + 四位年份**，例如 `092026` 是 2026 年 10 月。

同一开发机、配置代理的独立普通浏览器任务两次成功：
`discovery=SUCCEEDED`，`job_status=SUCCEEDED`，42 条 PriceHistory，
`provider_status=ONLINE`，`calendar_status=AVAILABLE`。测试数据库独立且无通知。
相同环境无窗口模式的酒店详情请求仍为 HTTP 403，因此 IHG 默认使用普通浏览器模式，
由 Worker 容器内部 Xvfb 提供显示器；不附着用户 Chrome，不复制用户 Cookie。
Linux 容器运行检查与群晖官网实采是不同验收项，不能由上述 Windows 成功推断完成。

金额存为 `cash_price`，`price_basis=nightly`，公开非会员价。
页面的 VAT Included 不能证明全球全部强制税费已含，故不捏造 `total_price`。
日历、异常判断、二次复查和 Telegram 支持该金额字段，但与含税费总价的历史序列隔离；
UI/消息明确提示“官网展示价，全部税费未确认”。
官网英文 hotel detail URL 必须与五位酒店代码匹配；网页可粘贴该详情链接创建资料任务。
全球酒店链接的自动发现仍未完成，当前目录需提供真实酒店详情 URL。

## 尚未开放预订的日期

2026-10-01 同一 LONLS 酒店真实查询 2027-09-28 / 09-29，官网明确显示
`Rooms cannot be booked this far in advance. Please change dates.`。
预订页此时清空搜索 URL 参数，但仍展示完整入住/离店年份、人数及酒店评论链接。
适配器核对这些公开页面信息后将日期记为 `NOT_OPEN`，不再等待不存在的房型而误报超时。
真实 Worker 复测：任务 `SUCCEEDED`、诊断 `BOOKING_WINDOW_CLOSED`、0 条报价、
集团 `ONLINE`、日历 `NOT_OPEN`；不生成零价或降价通知。
旧可售记录从当前日历移除但保留历史，后续正常轮询仍会重查该日期。

## 官网目录研究（未实现自动全量收录）

正常官网 `https://www.ihg.com/explore` 的全球酒店栏目含 9 个地区、246 个目录链接。
沿其中 Alabama Hotels 链接进入官网目录，页面标示 89 家酒店，公开 DOM 含
89 个不同酒店详情链接。目录起价没有指定请求日期，不能作为报价入库。
这些结果只证明可沿官网目录发现酒店，不代表全球收录或一年全量采价已完成。
