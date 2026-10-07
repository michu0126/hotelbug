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

2026-10-02 人数表单对照：正常官网的 `#compo-summary` 展开
`#booking-compo`，每房 `fieldset.booking__room` 中有实际 Add/Remove an adult、
Add/Remove a child 控件，添加第二房后两房均有 Remove the room。
成人输入的当前 `value` 属性为3时，其HTML初始value属性仍为1，房间数同样如此。
正常后退在一次观察中保留3成人，另一次重新加载后恢复1成人；不能依赖固定初始状态。
采集器改用 `input_value()` 读取当前值、使用真实按钮归一入住条件，删除额外房间时
从最后一房开始，避免首房编号改变。每步等待可见控件实际值，未改变时报错而非反复点击。

独立采集器0338、2026-10-20/21的真实公开含税方案：

| 成人 | SAVER RATE | FLEXIBLE RATE | SAVER 早餐 | FLEXIBLE 早餐 |
| --- | --- | --- | --- | --- |
| 1 | EUR 91.46 | EUR 96.21 | EUR 104.36 | EUR 109.11 |
| 2 | EUR 92.67 | EUR 97.42 | EUR 118.47 | EUR 123.22 |
| 3 | EUR 93.88 | EUR 98.63 | EUR 132.58 | EUR 137.33 |

只读取Public rate，不取更低Member rate。3成人与普通Chrome页面一致；没有提交预订或支付。
连续测试仍未全通过：3→1→2的第三次、2→3的第二次均在booking navigation超时。
第二次诊断确认停留酒店页，20/10/2026、21/10/2026、日期aria-invalid=false、
1房3成人0儿童均正确，未证实不导航的全部原因。保留失效状态而不导入错误日期报价。
`smoke_accor_browser` 新增 `--adults`、`--repeat-adults`，错误只输出公开命名表单状态，
不输出Cookie、隐藏输入、存储或鉴权头。14项离线状态测试通过，连续真实稳定性仍待完成。

2026-10-02 进一步排查，超时状态只存在原酒店页，form target为空，原生无效控件为零。
被动浏览器日志有“Request failed with status code 403”，另有内部授权文档ERR_ABORTED，
没有实际HTTP响应到页面异常的关联，不能将两条记录归因为同一请求或断言是唯一根因。
诊断只读浏览器已经发生的事件，不直接请求内部授权资源、改动安全校验或复制用户会话。
诊断脚本逐查询清理错误，并在任务页重建后重新绑定事件，防止混合上一任务的错误。

另一个已证实的脚本漏采：2成人官网显示两个房型，只有默认房型的命名方案展开。
另一个房型仅显示Public rate from，点击该卡片正常“Choose this room”后才展开4个方案，
同时默认房型折叠。因此旧默认4条不能当作该酒店/日期所有房型方案数量。
新流程按实际房型卡片依次读取并保存每次公开页面快照，合并稳定身份报价；
不读summary起价，排除Member rate，不触碰Continue/预订/支付。
核对房型标题、卡片数量与最终列表，某房型加载失败不返回部分成功；
只设置共享等待时间，不截断前10房型。新增13项离线测试覆盖这一路径。

实采结果：0338、2026-10-20/21、2成人，两个房型各4方案共8条：
Standard Room with 1 double bed、Standard Room with one double bed and one single bed，
均为公开含税EUR92.67/97.42/118.47/123.22。独立浏览器适配器成功，
真实生产Worker在隔离SQLite/模拟队列测试也SUCCEEDED、8条历史、ONLINE、日历AVAILABLE。
仍非群晖或全球验收，不发送真实Telegram。完整335项离线回归、Ruff均通过，继续未发布。

## 2026-10-05 全球目录持久续跑与新酒店真实价格链路

新增`tests/audit_sitemap_catalog.py`，使用生产目录调度/Worker、独立持久SQLite及
FakeRedis，普通专属Chrome和现有代理；同时支持GHA/Hilton的目录验收入口，
本轮仅对雅高执行真实请求。不会运行健康检查、价格或通知投递，不删除历史或重置失败冷却。
专用数据库标记拒绝误用现有生产库，成功详情与此前验过的代码排除名单持久保存。

官网英文地图当前实际解析5900个候选（此前5899是旧时点样本，未直接沿用旧数量）。
首批跳过0338/B3N7/C6M1/B5L7，从官方顺序自动选择0339、0340；后续两个独立进程
继续选择0341、0343及0344，没有重新请求已成功详情或首报价。
最终保存5条目录资料，其中0339的官方名称明确为
`Test Hotel for the helpdesk resynch`。这是测试条目，不能算成真实启用酒店。
已按完整明确名称标记active=false，其他4条启用；不按任意“test”子串过滤，
普通刷新也不显式active=true重新启用用户停用酒店。国家/地址尚未解析，保持NULL，不根据名称猜测。

目录生产全年调度自动生成0340 Mercure Annecy Sud Hotel的2026-10-05/06、1房2成人
任务。`tests/audit_ihg_price.py --provider accor`只执行这个已有任务，不手动指定酒店或日期。
真实普通Chrome Worker一次SUCCEEDED，retry_count=0，StayScan=AVAILABLE，保存12条报价。
SQL核对6个不同房型、2个命名方案，全为非会员公开含税stay_total、EUR184.20–294.20，
cash_price/tax未分解保持NULL；不是目录起价、模拟报价或错误条件合并。
后续目录续跑保留这12条历史，未向Telegram发送行情。

实测测试条目还揭示了停用后旧价格任务仍执行的Worker问题。三个离线回归在修正前
均调用了Provider（FETCH_RATE/FETCH_CALENDAR/VERIFY_ANOMALY）；现执行前正常CANCELLED，
不发官网请求、不生成StayScan或新价格、不删除旧历史。实际验收库0339此前自动生成的
PENDING价格任务已由同一Worker正常取消，message=Hotel disabled，而非手改成功/失败或删除证据。

新版目录与价格验收工具可以续跑，但不能把5900个候选或4家启用酒店当成全球全量。
当前为Windows/SQLite/FakeRedis验证，不是最新Linux/PostgreSQL/Redis/NAS运行或真实Telegram送达。
没有Git提交/推送、CI、DockerHub或NAS更新。

## 2026-10-05 新酒店自动报价与地区字段（本地，未发布）

目录自动生成的0343 Mercure Annemasse Porte de Genève Hotel任务
9c87a173-ce1a-4396-a4c3-f42b1bf2e9df一次SUCCEEDED/retry0/AVAILABLE，
2026-10-05/06、1房2成人，5房型/2命名公开方案，共10条含税EUR133–202的stay_total历史。
与原0340的12条合计22条，非目录起价或会员价格，未知cash/tax保持NULL；未重复成功报价。
数据库中的Genève字符正确，部分Windows终端输出乱码不是数据库损坏，不改写酒店名。

持久目录正常续跑另6个新详情0345、0348、0349、0350、0351、0354，最终10条启用真实酒店
及1条停用测试条目，候选仍5900、游标12。原地图及成功详情不重读，价格/通知增量为0。

实际0345公开Hotel JSON-LD有streetAddress/addressLocality/addressCountry及geo，但旧
详情采集只取h1，导致资料漏收。现同一已加载页面读取公开Hotel schema，按确切官网酒店
路径和酒店名核对，不读取应用私有状态/价格接口。缺少字段不推断、旧资料不清空。
实际0348/0349部分schema名称是SEO标题；其不同标题不能按酒店名子串猜匹配。
已加入同页Hotel location的p.infos__title确切酒店名及相邻地址对照，要求schema街道和城市
均出现在这一个地址块。只从Hotel schema保存该地址的原始国家码（如FR）、城市和有限坐标，
不把附近车站/景点、页面全局文本、不同酒店或歧义多条Hotel schema拿来拼资料。

新真实0350、0351、0354已保存FR/BEAUVAIS、FR/PARIS、FR/WOIPPY及公开街道/坐标。
旧0345/0348/0349资料仍NULL，等待正常刷新，不重复成功详情来人为补验。
无迁移/新必需环境变量；相关167项离线回归、Ruff及131文件格式检查通过。
本轮未重跑旧完整852项或前端，仍不是全球全部目录/全年/NAS/真实Telegram验收。
## 2026-10-05 勒阿弗尔新酒店自动报价（本地，未发布）

既有自动任务61ef5772-231e-4de8-8c68-329b60c3f9f9，Mercure Le Havre Centre
Bassin du Commerce0341，2026-10-05/06、1房2成人，一次真实Worker
SUCCEEDED/retry0/AVAILABLE。2房型/2命名公开方案，共4条EUR178.72–246.72
含税单晚住宿总价，price_basis stay_total，cash_price/tax未知NULL，member_rate false。
同专用库历史增至26，旧Annecy/Annemasse成功价格不重抓，NotificationLog0。
首次观察不冒充已确认异常；没有真实Telegram、Git推送、CI、DockerHub或NAS更新。

## 2026-10-05 到期复查与Auxerre新酒店（本地，未发布）

本轮默认队列原先选择到既有06d9d1ad-0609-49ed-9224-4ecafa98ad60：Annecy0340正常
HOT到期复查，任务21:19UTC已由3小时调度生成（原观察17:40），22:06执行成功。
这是正式监控需要的定期复查，不是新酒店；12个相同房型/方案展示总价差均0，但取消字段
从true变为NULL，完整offer_key不同，不能当成12个同条件报价。尚无本次取消说明DOM证据，
不猜是条款改变、截止已过还是解析问题。两组历史分离，不合并/补写退款资格或旧数据。
新12条观察真实保留，没有修改/删除旧12条、伪造历史日或发送Bug通知。

为避免开发验收默认再选已观察的条件，工具选择现检查StayScan酒店/日期/人数/房间数，
跳过已观察的活跃任务但不取消它、改其截止或正式调度。只有明确--allow-due-recheck
才允许测试现有到期监控任务；未观察新日期/不同人数照常自动选择。
新增3项离线回归，工具及正式重查相关47项通过（4.98秒）。

该保护后的默认工具选中原既有新酒店任务6c375ecc-5060-4b90-9af2-b3fdc0a0aae3，
Hotel Mercure Auxerre Autoroute du Soleil0348，2026-10-05/06、1房2成人。
真实Worker一次SUCCEEDED/retry0/AVAILABLE，2房型/2公开方案，4条含税EUR140.50–180.30
单晚住宿总价；未知cash_price/tax NULL，member_rate false。专用库累计42条，NotificationLog0。
未重复其他成功样本，无Git提交/推送、CI、DockerHub、真实Telegram或NAS更新。

## 2026-10-05 新目录、Woippy报价及同页条款诊断（本地，未发布）

0355、0356、0357、0358、0360、0363六个新详情均SUCCEEDED，累计16家启用及1条停用测试
条目/5900候选，游标18。原自动任务5e669244-cb69-429d-9144-bc36a1a29ab7选择ibis Metz
Woippy0354，2026-10-05/06、1房2成人，真实Worker SUCCEEDED/retry0/AVAILABLE。
4房型/2命名公开方案，8条EUR117.74–157.74含税单晚住宿总价，cash_price/tax未知NULL，
member_rate false，refundable未知NULL；累计历史50，通知0。

可选--stay-diagnostics只读该次项目浏览器同页可见公开方案文本，不导航或读取私有状态。
当次最后可见两个Comfort方案明确显示Cancellation with fees applies from 5th Oct 23:59、
No prepayment required；没有原解析器识别的Cancel free of charge原文。未知仍NULL，
不能凭Flexible方案名或未付款推断资格，也不能把这家新酒店的文本倒填到Annecy旧观察。
4项离线诊断回归覆盖原错误保留、诊断失效及非官网报价页跳过。

该次任务已成功提交8条历史，但打印结果时房型里的U+2060无法编码为Windows GBK，工具
退出1；持久库确认真实任务SUCCEEDED而不是抓取失败。工具起始/结果JSON改ASCII转义，
Unicode内容完整保留，新增GBK输出回归；未重新抓取成功任务或改写原历史。

## 2026-10-05 十个新详情及Lyon Sud Vienne报价（本地，未发布）

0364、0368、0369、0370、0371、0372、0373、0374、0375、0376十个新详情全部SUCCEEDED，
累计26家启用及1条停用测试条目/5900候选，游标28。原未观察自动任务
0723317a-7eac-43e1-847b-141c82b2e7de选择ibis Styles Lyon Sud Vienne0349，
2026-10-05/06、1房2成人，一次SUCCEEDED/retry0/AVAILABLE。
3房型/1公开早餐方案，3条EUR144.32–166.32含税住宿总价，cash_price/tax未知NULL，
member_rate false，专用历史53、通知0。旧成功价格不重抓，Windows输出修正不影响Unicode
酒店/房型身份；资料未确认的国家字段没有靠新价格或酒店名倒填。
