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
单酒店资料任务也支持提供真实酒店详情 URL；新增全球官网目录任务可自动发现酒店。

## 尚未开放预订的日期

2026-10-01 同一 LONLS 酒店真实查询 2027-09-28 / 09-29，官网明确显示
`Rooms cannot be booked this far in advance. Please change dates.`。
预订页此时清空搜索 URL 参数，但仍展示完整入住/离店年份、人数及酒店评论链接。
适配器核对这些公开页面信息后将日期记为 `NOT_OPEN`，不再等待不存在的房型而误报超时。
真实 Worker 复测：任务 `SUCCEEDED`、诊断 `BOOKING_WINDOW_CLOSED`、0 条报价、
集团 `ONLINE`、日历 `NOT_OPEN`；不生成零价或降价通知。
旧可售记录从当前日历移除但保留历史，后续正常轮询仍会重查该日期。

## 全球官网目录接入（持续发现，尚未全量验收）

正常官网 `https://www.ihg.com/explore` 的全球酒店栏目含 9 个地区、246 个目录链接。
沿其中 Alabama Hotels 链接进入官网目录，页面标示 89 家酒店，公开 DOM 含
89 个不同酒店详情链接。目录起价没有指定请求日期，不能作为报价入库。
这些结果只证明可沿官网目录发现酒店，不代表全球收录或一年全量采价已完成。

新增 `DISCOVER_CATALOG`：Scheduler 自动创建根目录及后续地区/城市任务，Worker
读取实际官网 HTML，按酒店详情链接和可见名称归一化真实 HotelData；公开 Hotel JSON-LD
仅在 URL 与可见酒店名均匹配时补充国家、地区和地址，不读取其中的起价作为报价。
`catalog:ihg` 在数据库保存所有发现目录、扫描时间、后续检查时间和完成/部分/失败状态。
调度遵循集团锁、间隔、暂停状态、活跃队列上限，失败页保留进度并后续重查。
目录合并使用数据库行锁；真实酒店直接进入现有全球一年日期调度。
网页与 `GET /api/catalogs` 区分收录酒店、取得报价酒店和目录覆盖情况。

真实独立 Worker 测试成功读取全球根目录、自动选择的 Acapulco 城市目录和
Alabama 目录，发现 292 个目录链接、入库 91 家不同酒店，目录阶段 0 条 PriceHistory，
并自动生成一个一年范围内日期任务。小城市页可能没有总数栏：不能等待不存在的控件；
它仍可产生真实酒店资料，但目录完整性标为未确认。全球其他目录尚未逐页验收。
随后 HSVPP 美国酒店报价任务在提交搜索阶段超时，查明是同名品牌页头按钮只滚动到
搜索表单，真正提交按钮为 `data-testid="consolidate-search-submit-button"`。
改用该公开表单按钮后独立复测成功：Holiday Inn Express & Suites Huntsville West -
Research PK，2026-10-20 / 10-21，1 房 2 成人，12 条报价入库，任务 `SUCCEEDED`、
集团 `ONLINE`、日历 `AVAILABLE`。这验证了第二个国家和另一个 IHG 品牌，
仍不代表所有全球酒店、品牌及日期全部验收。

修正后的完整 `smoke_ihg_catalog.py --directory https://www.ihg.com/alabama-united-states
--with-price --date 2026-10-20` 实测以 Exit 0 结束：三个目录任务和报价任务均成功；
292 个已发现目录、91 家酒店、目录阶段零报价、自动一年日期任务、最终12条真实报价。
测试使用独立临时数据库和队列，不向用户 Telegram 发送消息，也不代表群晖已安装。

本地未发布的全球目录审计：每页保存公开子目录和酒店代码，按当前全球入口可达图
计算有效读取、待查、失败、过期、未完整核对页面数，并对重叠酒店去重。
全球入口未公布可核对的酒店总数时，即使已发现目录全部遍历，也只标为
TRAVERSED_TOTAL_UNVERIFIED，不宣称全球酒店完整覆盖。旧页缺少审计信息会重新核对；
网页和 /api/catalogs 的目录覆盖标记与真实报价酒店数独立。

## 2026-10-05 可续跑全球目录实测

原测试临时库结束后无法继续旧游标。新增 `backend/tests/audit_ihg_catalog.py`，使用
独立 `catalog-audit.sqlite` 保存目录/任务/Hotel以及跳过既有成功样本的名单；没有删除旧库，
不运行报价、健康检查或Telegram投递。使用正常专用Chrome、现有代理与生产
`discover_ihg`/Worker事务。原入口索引只在此验收库的首次运行重新建立一次，后两次进程
续跑没有重新访问根目录或成功叶页，Acapulco/Alabama保留为未执行，不冒充已验完成。

新实际成功页：aguascalientes-mexico、alaska-united-states、albania、alberta-canada、
algeria、altamira-tamaulipas、angola、apodaca-nuevo-leon、argentina，全部来源于
官网读取的目录链接，非猜测酒店输入。共保存75个不同酒店代码，覆盖美国、墨西哥、
加拿大、阿尔巴尼亚、阿尔及利亚、安哥拉、阿根廷。官网Hotel资料与可见名称匹配才补充
地区资料；这不是查价成功，也不是NAS生产数据。两轮自动生成一年范围内查价任务，
未执行，零PriceHistory/NotificationLog。

当前图278页，读取10页（根目录+9页）、268页待查；新叶页没有可核对酒店总数，
状态明确PARTIAL，目录全量仍IN_PROGRESS。不能把75家与此前临时测试91家相加，
也不能将已访问页面数当成全球覆盖证明。专用验收库位于本地work目录、未提交。

这些实际部分页小时重查的状态揭示了发现队列的饥饿风险：原按插入顺序总取先到期的
旧页。已改为数据库持久化的未访问/重查交替通道，重查按最早到期排序；集团暂停、
任务去重和总积压上限不变。不要缩短官网冷却时间或用新任务绕过失败记录。

## 2026-10-05 新目录酒店真实价格任务与一位数日期修正

执行上述目录由生产全年调度自动生成的两个任务，未手动输入酒店：ALGCT
Holiday Inn Algiers - Cheraga Tower（阿尔及利亚）及YQUAB Holiday Inn & Suites
Grande Prairie-Conference Ctr（加拿大），均为2026-10-05/06、1房2成人。
两次正常表单查询都进入房型结果，旧参数校验却INVALID_RESPONSE：官网qCiD=05、
qCoD=06，代码只接受5、6。之前LONLS/HSVPP与远期测试用了20/21、28/29，未覆盖
这个日期格式，不能归咎于网络。第二次记录仅公开日期参数，未记录Cookie或完整URL查询。

校验仅对单一合法1–31日值进行数值规范化，同时保留酒店、月份/年份、人数、儿童、
房数和公开默认搜索方案检查；符号/空白/小数/三位填充/重复或不同日期仍拒绝，
页面显示日期与酒店也继续独立核对。后续诊断只显示合法数字日期，不回显非数字内容或
其他URL参数。没有弱化身份校验、换代理/通道、复制用户会话或寻找价格接口。

代码修正后分别正常重试这两个此前失败的任务，旧失败记录没有删除或重置。
ALGCT成功保存9个房型共18条公开方案，展示币种DZD，价格20000–94500/晚；
YQUAB成功保存7个房型共14条公开方案，CAD 249–398/晚。不同房型/方案不混合比较。
两者StayScan=AVAILABLE，32条全部非会员、1房2成人、nightly、cash_price金额，
全部强制税费未确认，total_price仍NULL。未再请求已成功的LONLS/HSVPP。

验收脚本audit_ihg_price.py只选已有到期自动价格任务，不运行投递；显式修正复测
仅允许该身份错误家族的原FAILED任务，不允许通过此选项重试429/403/超时，
活跃复测去重，原错误之后已有成功记录则禁止再复测。目录工具改为核对本轮价格/通知
增量为零，后续目录续跑保留这32条真实历史，不清空报价来满足断言。没有真实Telegram
消息，没有群晖部署，全球完整目录与一年全量价格仍未验收。

## 2026-10-05 持久目录继续扩展（六个新页面）

保持上述验收库及既有价格历史，正常发现/Worker依次读取此前未访问的Arizona、
Arkansas、Armenia、Aruba、Australia、Austria目录，六个任务均SUCCEEDED。
本次没有重读根目录、旧成功叶页或任何已成功报价；此前排除页名单从数据库自动恢复，
未重新提供CLI排除参数，也未改写失败或冷却状态。

同库不同酒店由75增至287（新增212），国家资料为Albania 1、Algeria 1、Angola 1、
Argentina 5、Armenia 2、Aruba 3、Australia 63、Austria 15、Canada 45、Mexico 17、
United States 134。这是酒店资料计数，不是成功报价酒店数。
目录图391页：完整页1、待查375、读取但完整性未确认15，全球仍IN_PROGRESS。
没有公开全球酒店总数，不把这些叶页SUCCEEDED解释为该国家或集团已经全量。

本轮目录新增PriceHistory=0、NotificationLog=0；既有真实价格历史32条完整保留。
正常全年调度再生成1个任务但未执行。不是Linux/PostgreSQL/Redis/NAS验收，
无真实Telegram、Git提交/推送、CI或镜像发布。

## 2026-10-05 新酒店自动任务诊断（本地，未发布）

执行同库既有任务6fbb576a-ee9c-4f31-a5eb-1039ec36fd69，YWCAB
Holiday Inn Express & Suites Whitecourt Southeast，2026-10-05/06、1房2成人。
首次TIMEOUT发生在selected stay confirmation；随后两次按原持久退避截止时间由同一
生产Worker重试，均在expand room plans超时。保持PENDING/retry_count3，原截止时间
2026-10-04 21:32:47 UTC；无新增PriceHistory或StayScan，32条旧历史和原日期修正失败保留。
第四次工具调用时该截止尚未到期，NO_PENDING_PRICE_DUE退出，无新官网请求或重置。

新可选--stay-diagnostics只读取同一次已加载页面的公开日期/人数/房型控件，不额外导航。
实际后两次日期10/05/2026、10/06/2026，人数1 Room, 2 Guests，均可见；有6个房型代码。
故不能将这两次故障解释为错日期、人数、无房或访问拒绝。原诊断仅按按钮innerText匹配
View prices for，未获得匹配；不能证明没有可访问名称不同的按钮。已增加同卡公开按钮
text/aria-label/title与可见/禁用状态诊断供正常下一次重试，不声明尚未取得的结果。
诊断失败不会覆盖原Worker错误，测试确认不会打开第二页或输出URL私密参数。
目前未改生产IHG选择器；原因仍待真实控件核对，不能因其他酒店通过而宣称全集团稳定。

## 2026-10-05 直接展示方案修正及实采（本地，未发布）

原任务第四次正常到期执行仍在expand room plans超时，最终FAILED/retry_count4；
同页公开控件显示6房型均直接展示Best Flexible Rate和Select，没有View prices for按钮。
这不是HTTP拒绝或无房。生产流程现有展开按钮才点击，否则等待本房型实际rateCard。
仍必须关闭同房型会员开关、等待旧会员报价消失、重新核对酒店/全日期/人数和方案代码；
不点击Select，不把目录起价或会员价当公开报价。

原任务6fbb576a-ee9c-4f31-a5eb-1039ec36fd69保留FAILED；在其原持久截止时间之后，
一次明确代码修正复测73289d3a-475e-4849-b047-7106133000ea SUCCEEDED/retry0/AVAILABLE。
YWCAB同2026-10-05/06、1房2成人，6房型各1个Best Flexible Rate/IGCOR：
TQNN169、KNGN179、CSTN189、KFTN/XFTN/XSTN209 CAD每晚。
实际页面明确Excludes taxes，故保存cash_price，tax/total_price均NULL，会员标记false。
专用库历史从32增至38，NotificationLog0。原失败及截止未重置，成功后工具拒绝再造复测。
新增实际公开字段投影和有/无展开按钮流程回归；本地实采不代表全球/NAS稳定性验收。

## 2026-10-05 目录计数渲染时序修正（本地，未发布）

本轮同一专用库正常续跑14个目录任务，含此前部分页的到期补查，不能说成14个新目录。
新增地区包括Azerbaijan、Bahamas、Bahrain、Bangladesh、Barbados、Belgium、Bhutan。
酒店资料由287增至317，目录图406页：完整2、待查383、完整性未确认21。
目录无新增价格/通知，原38条真实价格历史保留；全球仍IN_PROGRESS。

同页公开诊断发现Alberta酒店链接已45个，但生产第一次读取时总数还为空，诊断随后已45。
旧ready条件只等待首个h4链接，使已读全页误判总数未知、每小时重复补查。
现在仅对存在公开计数、且计数未渲染或卡片尚少于公布数量的页面，有限等待5秒；
计数完全缺失不额外等，始终为空则保留部分结果。等待后再次检查拒绝页/URL身份，
不点击Select/登录、不求接口，不把未知/外站/不支持的酒店链接当成完整；未知链接数如实保存。
不会为小城市缺失公布总数制造数量，也不强行把Featured Hotels当全球全量。

修正后的正常新目录Belgium任务266178da-4447-4ef7-b200-3b3059e0f453 SUCCEEDED，
实际公开19与解析19一致，页面完整标记true，按正常目录周期刷新而非小时补查。
其他无总数页面仍未确认；没有为了修正规则强制重置旧页时间、重复成功价格或修改旧报价。
新增13项离线控件诊断、漏解析计数及计数/卡片迟到、永久空、无计数、重定向和拒绝页回归，
相关166项测试通过（8.81秒），代码检查通过；最新代码仍未发布或在NAS更新。

## 2026-10-05 二十个目录任务续跑（本地，未发布）

同库生产目录调度继续20个任务，全部真实SUCCEEDED，包含Apodaca、Argentina、Arizona、
Arkansas、Armenia、Aruba、Australia、Austria旧部分页到期补查，以及Boca del Rio、
Brazil、British Columbia、Bulgaria、Cabo San Lucas、California、Cambodia、Campeche、
Cancun、Cayman Islands、Celaya、Chihuahua新页面；不是20个全新页面。
收录从317增至592个真实酒店，实际国家字段21个；目录图616页（完整13、待查581、
未确认22），没有公开全球总数且图未完成，仍IN_PROGRESS。
目录计数渲染修正使一批旧部分页正常刷新后进入完整状态，不强制重置时间或篡改旧页。
本轮目录新增PriceHistory0、NotificationLog0，原38条实际历史保留；全年调度新增1任务，
生成任务不等于执行或一年覆盖。没有发布、真实Telegram或NAS操作。

## 2026-10-05 后续二十页（本地，未发布）

正常目录调度继续Chile到Durango二十个任务，全部SUCCEEDED。持久库酒店从592增至776，
已知国家29；目录图711页（完整22、待查656、未确认33），全球仍IN_PROGRESS且没有公开
全球总数。原38条价格保留，目录新增报价0、通知0；未更改暂停/截止或重抓成功价格。

## 2026-10-05 四十页续跑及Yuma新报价（本地，未发布）

两批各20个正常目录任务（Ecuador到Guam、Guanajuato到Jamaica）均真实SUCCEEDED，
776增至1477再至1981家/53个已知国家；目录图1368页（完整38、待查1273、未确认57），
全球仍IN_PROGRESS。新增默认任务f5865943-d6c8-4790-8825-759ccfadcc89选择Holiday Inn
Express & Suites Yuma YUMYA，2026-10-05/06、1房2成人，SUCCEEDED/retry0/AVAILABLE。
2房型/1Best Flexible Rate IGCOR，USD309–314每晚cash_price，tax/total_price未知NULL，
member_rate false，专用历史40、通知0；旧成功任务未重抓。

同页目录诊断现投影不支持的酒店名及公开主机/路径，不记录query值、私有输入或Cookie。
真实India（Fort Barwara、Vana）、Indonesia（Bali）、Israel（Shaharut）、Italy（Rome）
六善酒店的链接指向www.sixsenses.com而非常规IHG酒店代码路径，尚不能收录到现适配器。
不直接探查跳转接口、不虚构IHG代码、不把它们静默删去；未解析计数和PARTIAL保留。
缺少公开计数的页面仍未确认，且不能据这些六善样本推断France那个未解析链接的身份。

## 2026-10-05 华邑路径与六善公开跳转（本地，未发布）

Japan一页及Jordan到Maldives二十页正常目录任务SUCCEEDED，累计2548家/67个已知国家，
1797页（完整51、待查1681、未确认65），原40条报价保留，目录新增报价/通知0。
真实Mainland China计数200+、200酒店链接，14个华邑链接形如/hotels/us/en/beihai/bhybr/
hoteldetail，没有品牌前缀。PROPERTY_PATH新增可选品牌段，四个捕获位置不变、代码仍
严格五位，缺失品牌NULL。新增19项实际路径/来源/代码及正常详情流回归。
标准项目会话实读北海华邑详情，酒店名与BHYBR确认，未写入价格或篡改旧目录截止。
原大陆页仍PARTIAL，后续正常刷新才可补收此前漏解析条目；未声称14家已经入库。

可选--follow-six-senses只点击正常任务同页实际发现的一个公开六善酒店链接，持久记录
尝试，跨批次不重复旧记录；不直接请求跳转接口、发送预订、读Cookie或写价格。
首次Kyoto没有定位到可见唯一链接，没有点击；改为匹配实际酒店标题文本后，Maldives
新页Laamu链接一次正常跳转到官方住宿页，PUBLIC_PAGE_READ，公开BOOK NOW控件可见。
这仅证明公开跳转，不是报价成功或六善酒店代码映射。13项链接可见性/唯一性、官网边界、
拒绝、popup关闭、持久不重试及投影回归；facebook不再因book子串被归类为预订控件。

## 2026-10-05 二十页续跑、新报价与六善预订控件（本地，未发布）

正常目录任务20个成功，包括10个旧部分页到期补查和10个新页，不是20个全新页面。
累计2682家/69个已知国家，可达1885页（完整59、待查1759、未确认67），全球仍IN_PROGRESS。
目录新增报价/通知0；随后原全年任务4a3306b6-35f6-4dc8-a2d8-d4e6d4abce31选择LKFCA
Staybridge Suites Irvine East/Lake Forest，2026-10-05/06、1房2成人，SUCCEEDED/retry0/
AVAILABLE。6条Best Flexible Rate公开非会员USD159–279每晚cash_price，tax/total NULL，
专用历史40增至46、通知0，未重复此前成功酒店日期。

六善普通官网流程单独持久记录三个阶段，重复启动不再请求旧阶段。首次标题判断代码
错误在BOOK NOW点击前发生；修正异步标题读取并保留原TypeError后，初始控件可读。
接着DOM-ready、正常点击可见ACCEPT与BOOK NOW，有限等待仍没有可见日期表单或Synxis
iframe，结果BOOKING_FORM_NOT_VISIBLE，而不是已查询价格。下一诊断阶段只记录公开
script加载失败/HTTP错误与首行页面脚本错误，忽略fetch/xhr、Cookie、请求头和query值。
同次观测有一项identity.sixsenses.com脚本失败，但未取得网络失败类型，不能证明因果、
不能据此认定路由器代理有故障，也没有探查该脚本或站点接口。
后续诊断代码只保存合法net::ERR_*网络标识，任意错误文本保持UNKNOWN；没有为了新字段
重跑这张页面。16项控件/错误类型离线回归，完整后端986项通过。仍没有六善日期报价、
官方酒店ID映射或生产适配成功证据，不将公开页面点击计为酒店/PriceHistory。

## 2026-10-05 后续目录及公开地图来源（本地，未发布）

再20个正常目录任务全部成功，含10个旧部分页到期补查与10个新页面；累计3069家/71个
已知国家，2106页（完整67、待查1970、未确认69），全球仍IN_PROGRESS。目录新增报价/
通知0，原46条价格保留，没有重复成功价格或重置旧目录截止。

实际公开robots.txt HTTP200列出/bin/sitemapindex.xml与/services/sitemaps/sitemap-index.xml。
只读取前者一次，HTTP200、7923个loc，含多品牌/语言/页面类型，不是7923家酒店。
正常读取其实际链接/bin/sitemap.holidayinnresorts.en-us.hoteldetail.xml一次，HTTP200、
601个loc，其中展示英文酒店详情URL和五位代码。这些是未经详情核对的公共候选，不能
根据地图文件或URL前缀推断酒店真实品牌，也没有直接入库或调用报价接口。
该阶段补充来源尚未接入生产Scheduler和持久化地图游标，后续需完成官方详情身份核对、
跨地图代码去重、与现有目录公平预算/队列去重及共享暂停后才可证明自动补采。

## 2026-10-05 官方地图生产补采（本地，未发布）

现已接入Scheduler：catalog:ihg-sitemap独立保存地图/缓存/游标，与原catalog:ihg浏览器图
按持久source-lane交替，同一集团锁、预算、队列容量和401/403/429暂停。26张英文详情图
来自实际/bin/sitemapindex.xml，不读取预订接口。跨图五位代码去重，近期已有酒店、停用
酒店和在途详情跳过；陈旧资料仍可正常刷新。候选始终通过原Worker公开详情核对后入库。

生产路径的独立验收工具要求原IHG隔离库标记；正常持久续跑两次，未重读成功详情。
首次armyhotels图61个候选，41577ebe对应ZYAPB真实标题Candlewood Suites Building 2250；
第二次读avid图，累计79个URL、游标2，b3fe21ba对应MFHAM真实标题Holiday Inn Express
Building 107。两项SUCCEEDED，3069增至3071家，国家未知保持NULL，不据US路径补国家。
原浏览器图仍2106页（完整67、待查1970、未确认69）；地图候选/游标不是价格覆盖，Army
Hotels公开资料也不是一般旅客订房资格证明。目录报价/通知0，原46条报价保留。

API/UI独立展示地图总数/已读/缺失/失败、代码去重候选及经过的原始游标位置，不覆盖
原地区图或酒店计数。25项补采、5项验收工具回归；前端实际SFC三项SSR验证状态为空隐藏、
数字语义及错误文本转义。完整后端1067、前端28和构建通过；代码未提交、镜像未发布。

## 2026-10-07 续跑与无房列表漏判（本地，未发布）

地图第三张正常到期读取，177原始候选、游标3，bf033dd7对应ZYJIA官方详情成功，
累计3072家；目录新增报价/通知0。随后由原全球日期游标生成16c67e30，LITLA
Holiday Inn Express & Suites Lonoke I-40 (Exit 175)，10-07/08、1房2成人，一次
SUCCEEDED/retry0/AVAILABLE，5房型/Best Flexible Rate，USD134–149每晚现金价，
tax/total NULL。历史46增至51、通知0，不重复旧成功日期。

原DENLT 10-05/06任务c6357a47最终TIMEOUT/retry4。公开同页诊断实际显示正确日期/人数，
跳至hotel-search，目标app-hotel-card-list-view id DENLT、brandHotelNameSID酒店名、
Hotel website链接代码及No rooms available for selected dates；附近酒店只有From起价。
旧适配器只等房型，漏掉这种明确无房跳转。现只在目标唯一可见卡片及精确可见提示、
名称/官网详情代码/URL日期人数/显示日期人数一致时返回空报价，Worker记录UNAVAILABLE。
其他酒店或全页无房文字、错误身份/隐藏提示不算无房，列表起价永不入库。

首次修正复测b3778a2a保留原截止，实际止于INVALID_RESPONSE官方链接身份检查，零历史。
最后修正仅把已渲染公开Hotel website路径作为身份（允许附带字段，不导航该链接），
保持目标代码/名称和当前查询条件校验；此改动离线通过，但原10-05条件已经过期，不能
再次创建旧日期实采或宣称该分支真实成功。原失败保留。验收工具默认选择当前365天内
未观察条件，显式旧任务仍由生产Worker取消，代码修正工具拒绝重建过期失败。

## 2026-10-07 地区目录回退首页（本地，未发布）

上轮13个正常目录任务成功后，资料3191家，目录图2163页仍未完整。新喀里多尼亚
15dd0f49原FAILED/INVALID_RESPONSE缺少跳转目标；一次有持久标记的普通Worker诊断
dba7798d实读最终https://www.ihg.com/explore，标题Where to Next: Year End Edition。
全球首页17个酒店链接不是该地区酒店，不能归入地区或推断区域无酒店。

生产现在将这一已观察到的“地区→同站全球探索首页”明确分类为DIRECTORY_REDIRECT，
其他未知跳转/域名/附带字段仍维持身份失败，403/429仍保持原共享拒绝/限流类别。
Worker保留FAILED而非伪装成功，保存redirected_to，按配置的正常目录刷新周期再核对，
不每小时反复读取同一回退页。原页曾有资料时不删除历史，但不将旧酒店/子图计为当前覆盖。
覆盖统计继续显示失败及未完整，新增官网回退计数；新来源恢复时正常后续任务可清除状态。

代码修正后同代理/普通Chrome/原专用会话一次正常复查a8b4191c，实证FAILED/DIRECTORY_REDIRECT，
目标explore，下一核对2026-10-21 06:22:10.071718 UTC，原两个失败任务的错误/重试/截止不变。
真实库酒店3191、报价51、通知0不变。新增18项离线回归；最终完整后端1185项、前端33项
及构建通过。没有将离线结果或回退诊断当作区域覆盖、报价成功或全集团/NAS验收。

## 2026-10-07 一年窗口末端与地图续采（本地，未发布）

官方地图续跑到4/26、623原始候选URL、游标4，ZYKJA新详情一次成功，资料3192家。
Army Hotels资料不代表普通旅客可订或报价验证；原目录图仍未完整。

显式--year-boundary从既有正常全球任务选中WLWCA，Holiday Inn Express & Suites Willows，
新条件2027-10-06/07、1房2成人；不修改原任务、每酒店日期游标或全局轮询位置。
正常Worker任务14bd9c59-43f7-41a0-8b78-693f8027cd6a一次SUCCEEDED，官网匹配身份、
日期/人数及明确未开放预订消息，BOOKING_WINDOW_CLOSED/StayScan NOT_OPEN；报价0，
原历史51和通知0不变。NOT_OPEN不等于无房，更不填零价；正常生产周期后可复查。
工具持久记录这次边界尝试，不重建已结束条件，也不缩短失败重试截止或共享暂停。
这是单家末端日期链路证据，不是365日期或全球目录已完成。新增14项离线边界回归，
完整后端1219项通过（58.17秒）；未发布、更新NAS或发送Telegram。
