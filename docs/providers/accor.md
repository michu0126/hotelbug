# Accor 研究记录
状态：已实现官网页面适配器并注册；多酒店真实自动采集已通过，全球目录尚未逐一验证。

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
Worker 镜像需要 Chromium，已加入独立 worker 构建/发布目标并通过 Linux 容器启动验证。

真实 Worker 集成实测：`tests.smoke_accor_pipeline` 使用真实官网适配器、任务处理器和隔离 SQLite，
酒店资料任务 SUCCEEDED，报价任务 SUCCEEDED，写入 4 条 PriceHistory，Provider 为 ONLINE。
不向 Telegram 发送测试行情。Windows 已安装 Chrome 的实测通过；本机 Playwright Chromium
启动报 spawn UNKNOWN，故开发机实测使用安装版 Chrome；Linux Worker 镜像的浏览器启动已由 CI 验证，真实 NAS 网络抓价仍需单独验收。

2026-09-30 追加抽查：官网目录代码 B3N7、C6M1 在 2026-10-08/09 均读到 4 条公开含税费方案。官网新加坡 Pullman Singapore Hill Street（B5L7）同日期读到 2 条；页面当时以 EUR 展示，按原样保存而不猜测本地币种。B5L7 在接近一年上限的 2027-09-28/29 仍读到 4 条真实报价。该远期日期的隔离 Worker 首次运行一度 TIMEOUT，再运行成功入库 4 条、Provider ONLINE；现有 Worker 会对 TIMEOUT 自动退避重试。以上证明这些样本与远期日期可查，不等于全部 5,899 个目录候选均有价。

2026-09-30 会话复用回归发现实际脚本问题：先读酒店详情、关闭任务页、同集团下一任务继续查询时，延迟出现的 OneTrust Cookie 提示遮挡入住人数按钮。真实错误调用栈确认 `onetrust-consent-sdk` 拦截点击。现为每个新任务页注册官网 `Continue without Accepting` 控件处理，弹窗出现时正常点击关闭；未用强制点击或删除官网 DOM。隔离 Worker 的 B5L7 2026-10-20/21 最终成功写入 4 条报价、ONLINE、日历 AVAILABLE（公开含税 EUR 256.51/289.60/306.15/339.25）。仍观察到一次结果加载阶段的间歇性 TIMEOUT，不能声明网站完全稳定。生产错误信息现在保留脱敏后的失败阶段，避免只有 TIMEOUT 代码。

2026-10-01 `smoke_sitemap_pipeline.py --provider accor` 使用真实官网 sitemap 和生产目录
调度器自动选中0338，资料任务确认 ibis Alès Centre-Ville。由生产 `expand_global` 自动生成
2026-10-01 / 10-02的任务，没有手工指定酒店代码或日期。首次报价在 booking navigation
阶段 TIMEOUT，后续完整自动链路实测成功，入库2条报价。现场没有捕捉到首次超时的页面，
因此不能据第二次成功确认首次超时的全部原因。
导航等待现显式使用 commit，然后等待实际公开方案，仍核对酒店、日期、人数、币种及含税说明。
修订后再次执行自动链路成功：发现与报价均 SUCCEEDED、retry_count 0、2条 PriceHistory。
脚本对 TIMEOUT/NETWORK_ERROR 保留同一任务并等待生产 scheduled_at 后重试，不提前清空退避。
测试使用临时数据库与队列，目录起价不入库、不向 Telegram 发送行情，不代表群晖或全球全量实采完成。
