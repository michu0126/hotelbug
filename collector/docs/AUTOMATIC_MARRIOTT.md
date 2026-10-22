# 万豪自动查价与持续监控

`passive_capture.py` 只监听您手动查价时的响应；新的 `automatic_monitor.py` 会另外启动自动操作：依次打开官网酒店介绍页、选择入住和退房日期、设置人数、点击“查看房价”。价格仍然来自网页自己发出的响应，不直接重放接口请求。

## 目前运行在哪里

运行在安装了 Chrome 的这台 Windows 电脑上，不是在群晖容器里。使用本机 Chrome、有头模式、独立持久化目录 `collector/.profiles/marriott-passive`，通过只监听本机的调试端口 `9222` 连接。已打开的该独立 Chrome 可以继续复用；它不是日常 Chrome 的默认个人资料。

自动查价使用自己的标签页，不接管您原有的查价标签页。程序停止时只断开连接，不关闭 Chrome，也不操作群晖 Container Manager。保持电脑和该 Chrome 运行，才能持续查价。

如果官网日历尚未开放较远日期，任务会保留到下一轮再查，不把它记成无房或零价格。
滚动 365 天是待查询范围，不表示官网已开放全部日期。已执行的二次确认会记录覆盖时间，
房型消失或售罄时不会每隔十几秒重复同一复核、阻塞其他日期。

## 启动

在项目根目录打开 PowerShell：

```powershell
cd E:\Codexspace\hotel-bug-watch
.\work\collector-venv\Scripts\python.exe -X utf8 -u .\collector\automatic_monitor.py
```

不需要先手动启动被动监听器。程序会启动缺少的响应监听和价格检测；如果同一目录的监听或检测已在运行，就复用已有进程，不会重复采集或强行关闭它。

只运行一个到期任务：

```powershell
.\work\collector-venv\Scripts\python.exe -X utf8 -u .\collector\automatic_monitor.py --once
```

本次最多尝试 3 个任务，之后停止自动操作：

```powershell
.\work\collector-venv\Scripts\python.exe -X utf8 -u .\collector\automatic_monitor.py --max-queries 3
```

查看进度，不打开浏览器，也不影响正在运行的任务：

```powershell
.\work\collector-venv\Scripts\python.exe -X utf8 .\collector\automatic_monitor.py --status
```

`tasks` 是当前清单和日期窗口中的任务总数；`visited_tasks` 是至少成功查询过一次的任务数；`successful_queries` 包含后续复查次数。`states` 中 `WAITING` 是等待首次查询、复查或退避，`RUNNING` 是当前任务，`FAILED` 是已经达到重试上限。`paused` 表示整个万豪品牌是否已暂停。任务数不是已经获得价格的天数；明确售罄也会完成一次查询，但不会产生价格。

按 `Ctrl+C` 停止即可。下一次执行相同启动命令会复用 SQLite 任务进度，不会从头重新扫描所有已完成日期。意外退出后的在途任务在租约到期后恢复。

如果在后台运行，也可以在项目根目录执行安全停止命令，不必关闭 Chrome 或结束其他 Python 程序：

```powershell
.\work\collector-venv\Scripts\python.exe -X utf8 .\collector\automatic_monitor.py --stop
```

最迟在下一次任务间隔检查时退出（默认最多约 30 秒）。再次执行启动命令即可续跑；官网 403 的暂停仍需明确恢复。

## 配置酒店与日期

编辑 `collector/config.automatic.yaml`：

```yaml
days: 365
nights: 1
adults: 1
min_delay_seconds: 15
max_delay_seconds: 30
revisit_seconds: 86400
hotels:
  - hotel_id: XIYBP
    name: 西安钟楼福朋喜来登酒店
    hotel_url: https://www.marriott.com.cn/hotels/xiybp-four-points-xian-bell-tower/overview/
```

每间酒店每天一个入住日，所有任务串行执行。同一成功任务在 24 小时后重新查询；新的一天会补充窗口末尾日期，过去的日期不会继续执行。`days: 365` 表示滚动的一年日期范围，不代表官网一定已经开放所有日期售卖；多晚住宿的退房日也必须落在该范围内。

可以加入其他万豪酒店，但必须填写该酒店的真实中国站介绍页地址与对应的五位代码，不填查价入口、支付页面或接口地址。修改配置后停止并重启自动程序，使新清单生效。

这份配置当前只有 XIYBP。该自动操作适配器针对万豪中国站；尚不能据此声称万豪全球全部酒店或 IHG、希尔顿、凯悦、GHA、雅高已完成自动采集。

## 如何认定查询成功

普通有价结果必须同时满足：

1. 自动操作确认官网响应对应当前酒店、入住/退房日期、人数。
2. 被动监听器保存一份本次新响应，带独立采集事件 ID。
3. 样本解析器验证完整结果，核对站点、酒店、日期、人数、报价数量及响应内容摘要。
4. 历史检测进程已将该事件入库，然后才把任务标为完成。

以前保存的文件、同一事件的副本、仅点击了按钮、超时或不完整分页结果，都不能冒充本次查询成功。公开价和会员价、不同币种、人数、房型、房价方案和取消政策仍分开比较。

官网明确的售罄跳转与对应酒店卡片相符时，记录为 `NO_AVAILABILITY`；完整有效响应没有现金报价时记录为 `NO_CASH_OFFERS`。这两种状态都不会向历史写入“0 元”，也不会因此触发降价提醒。

## 降价复核与通知

自动任务连接现有 `collector/config.passive.yaml`：

- 同一个可比较报价系列，默认累积至少 5 个不同采集日的数据，以各日最低价的历史中位数作为基准。
- 当前价比基准下降至少 35% 时，生成候选。
- 自动队列把该酒店和日期的独立复查提高优先级，但不早于默认的 180 秒确认间隔，也不绕过请求退避。
- 二次查询仍满足条件，才进入通知流程。默认 24 小时内同一报价系列不重复通知。

`config.passive.yaml` 默认 `notifications.channel: console`，只会在终端打印演练结果，不会发送到手机。需要真实推送时，沿用 `docs/PASSIVE_MONITOR.md` 中的 Bark 或 Telegram 凭据配置；不要把凭据写进酒店清单或提交到仓库。

## 暂停和失败

- 403、验证码或其他验证页面：立即停止该品牌，保存暂停状态，不自动绕过或重复尝试。
- 429 限流：停止该品牌，按终端提示等待官网恢复。
- 普通网络超时、未保存响应或未及时入库：持久化退避重试，默认最多 3 次；达到上限的任务标为失败，继续其他日期。
- 监听器或检测进程退出：停止当前自动操作，不在没有采集证据的情况下继续批量查价。

确认官网已恢复正常后，显式恢复暂停：

```powershell
.\work\collector-venv\Scripts\python.exe -X utf8 -u .\collector\automatic_monitor.py --resume-brand
```

该参数不会处理验证码，不会删除历史或重置已完成任务。达到重试上限的失败任务不会因为不断重启而无限重试。

## 本地文件

| 路径 | 内容 |
| --- | --- |
| `collector/config.automatic.yaml` | 自动酒店清单、日期窗口、间隔与重试设置 |
| `collector/config.passive.yaml` | 历史检测、阈值、通知设置 |
| `captures/marriott/` | 脱敏后的真实官网响应；旧样本不会自动升级为实采证据 |
| `collector/data/automatic-queue.sqlite` | 持久化查询队列、暂停状态和查询结果分类 |
| `collector/data/passive-monitor.sqlite` | 报价历史、降价候选、复核与通知记录 |

程序自带单实例锁：同一个 Chrome 端口只允许一个自动操作进程，同一采集目录只允许一个响应监听器。不要删除正在使用的锁或数据库来尝试强制启动第二份程序。

## 验证范围

离线运行测试使用脱敏样本、模拟页面驱动和真实 SQLite，覆盖文件匹配、入库确认、售罄分类、暂停/恢复、重试上限、监听器退出、全年日期任务以及复核优先级；这些测试不向酒店或通知服务发送请求。实际官网操作是否成功应以终端本次结果、捕获文件和数据库记录为准，不能仅以测试通过推断。
