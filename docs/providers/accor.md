# Accor 研究记录
状态：已实现官网页面适配器并注册；单酒店真实自动采集已通过，全球覆盖与 NAS 镜像待验证。

2026-09-29，开发机 Playwright Chrome 经用户提供的 HTTP 代理访问：
robots.txt → sitemap-fh.xml → sitemap-fh.en.xml，英文酒店目录包含 5,899 个链接。
目录仅表示公开酒店页面，不代表每家有可订房价。

真实页面流程：打开 https://all.accor.com/hotel/0338/index.en.shtml，等待
`#booking-date-in.hasDatepicker` 初始化，输入 07/10/2026 和 08/10/2026，
通过人数按钮添加第二位成人，点击表单 See rates。
官网自然导航到 `/booking/en/accor/hotel/0338`，异步加载后出现房型与报价。
没有直接调用报价接口或复用用户浏览器的登录会话。

已验证酒店：ibis Alès Centre-Ville。1间、2成人、1晚，EUR：

| 官网房价方案 | 公开含税总价 | 取消 | 早餐 |
| --- | --- | --- | --- |
| SAVER RATE | 96.47 | 不可退 | 未知 |
| FLEXIBLE RATE | 101.42 | 页面显示可免费取消 | 未知 |
| SAVER RATE- BREAKFAST INCLUDED | 122.27 | 不可退 | 包含 |
| FLEXIBLE RATE - BREAKFAST INCLUDED | 127.22 | 页面显示可免费取消 | 包含 |

网页同时显示更低的会员价 91.77 等；适配器明确读取 Public rate，排除会员价。
每个报价卡必须明确显示 Taxes and fees included、1 night 和对应成人数。
核对预订页酒店代码、页面入住/退房日期及当前显示币种。
房型/方案用可见名称生成稳定比较标识，前缀 `name:` / `public-name:`，不是声称官网原生代码。
基础价与税费未独立展开，cash_price/tax 保持空值；total_price 为明确含税费的公开价。
价格页未出现有效卡片时报告失败，不生成零价。

实现：`backend/app/providers/accor_browser.py`。
单次实采：`python -m tests.smoke_accor_browser --proxy http://代理地址:端口`。
Worker 镜像需要 Chromium，已加入独立 worker 构建/发布目标；尚未发布本次更改。

真实 Worker 集成实测：`tests.smoke_accor_pipeline` 使用真实官网适配器、任务处理器和隔离 SQLite，
酒店资料任务 SUCCEEDED，报价任务 SUCCEEDED，写入 4 条 PriceHistory，Provider 为 ONLINE。
不向 Telegram 发送测试行情。Windows 已安装 Chrome 的实测通过；本机 Playwright Chromium
启动报 spawn UNKNOWN，Linux Worker 镜像仍需独立 CI 验证，不能把两者混为一谈。
