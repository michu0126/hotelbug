# 万豪被动采集监控闭环

在用户已经成功取得官网房价的被动监听器上，增加自动目录入库、历史检测、独立确认、
持久化去重和 Bark / Telegram 通知。不是重新编写爬虫，也不使用付费价格接口或 HotelXray 后端。

## 一个命令启动

在项目根目录 `E:\Codexspace\hotel-bug-watch` 执行：

```powershell
.\work\collector-venv\Scripts\python.exe -X utf8 .\collector\passive_capture.py --launch --monitor-config .\collector\config.passive.yaml
```

如果旧监听器仍在运行，先在旧终端按 Ctrl+C 停止它，**不用关闭 Chrome**。
新命令会连接仍然开启的本机调试 Chrome；找不到才启动复用独立 profile 的 Chrome。
不要同时运行两个监听器。新版本对同一输出目录加进程锁；监控数据库也有独立进程锁。

看到“监听中”和“监控已启动”后，在该 Chrome 正常查询房价。
每次目标响应会保存 JSON，并在约2秒内自动处理；不需要手工导入或展开 Windows 通配符。
停止后历史、确认中的候选及投递记录保留；不会关闭用户 Chrome、修改 NAS 或解除自动采集器的403暂停。

## 默认配置和价格口径

`config.passive.yaml` 为新模块专用，不改原来的 mock/Hilton 配置。

- 数据库：`collector/data/passive-monitor.sqlite`；不会访问旧 NAS 数据库。
- 文件：项目根目录 `captures/marriott/*.json`。
- 监测范围：未来365天内的**实际捕获**日期，不代表主动扫描了全年。
- `hotels: []`：监测所有实际捕获的万豪酒店；填写清单后，只对清单酒店生成候选。
- 酒店名称来自清单的 `name`；未配置时明确显示酒店代码，不编造名称。
- 同来源站点、酒店、入住/离店、房型代码、方案代码、公开/会员、币种、房间数、成人数、
  预付要求及已知取消条件才能比较。金额使用 Decimal；比较含税费的整段总价。
- 税额缺乏独立证据时为未知，不使用“总价减房费”冒充官网税额。
- 没有精确取消截止时间/条款时显示“未知”，不当作不可退款或免费可退。

历史规则默认取过去30天、至少5个不同 **UTC采集日** 的同一报价每日最低价，再求中位数。
当前采集日不进基线；同一天刷新100次也只贡献一个每日最低价。
含税费总价不高于中位数65%时产生候选；没有5天历史时，不会凭一份文件声称“下降35%”。

冷启动如需绝对价格规则，可按自己的预算设置，例如：

```yaml
hotels:
  - hotel_id: XIYWH
    name: 你要监测的酒店名称
    currency: CNY
    max_total_per_night: 你的含税费每晚上限数字
```

以上是填写格式，必须将最后一行替换为真实数字再使用；项目不替用户猜测“好价”预算。
跨币种的绝对上限不会应用。多晚阈值为每晚上限乘以晚数，但不同入住/离店仍是不同序列。

## 确认和去重

第一次命中只在本地记录 PENDING，不向手机发“疑似”消息。
用户至少3分钟后正常再次查询，6小时窗口内仍满足第一次冻结的门槛，才进入 READY。
这个模块不会代替用户刷新、登录、重放接口或绕过验证。

新监听器每个实际浏览器请求分配独立 `capture_event`；同价新响应也保留。
重复文件、复制文件、同事件被重复处理都不会产生新观测。修改同一文件时间戳会成为事件冲突，
不是二次确认。事件标识是本地流程去重证据，**不是防人为伪造的加密认证**，不要导入不可信文件。

旧版没有事件标识的文件（包括此次 XIYWH 的第一份）只进入隔离的样本表，不触发候选、
不作为新事件的历史基线或二次确认。请从新监听器之后的正常查询开始积累监控历史。
过旧但带有效新事件标识的数据只补历史，不触发手机通知；未来时间会拒绝。

确认后若又观察到价格回升，待发消息会取消；迟到的旧低价文件不能逆转较新的回升。
同酒店/日期/人数/币种的多条确认合并通知，列出最低3条并注明其余条数。
同一报价严格24小时去重，期间再降10%也不重复发。
投递超时或中途退出保留 UNKNOWN；可能已送达的请求不自动重试。
缺少凭据不占冷却窗口，候选保留到报价过旧。

## 开启真实通知

默认 `notifications.channel: console`，只在终端演练，不调用任何推送服务。

Telegram：将渠道改为 `telegram`，在启动命令前设置：

```powershell
$env:TELEGRAM_BOT_TOKEN="你的机器人Token"
$env:TELEGRAM_CHAT_ID="你的Chat ID"
```

使用官方 [Telegram sendMessage](https://core.telegram.org/bots/api#sendmessage)，要求成功响应 `ok: true`；
不记录令牌 URL 或原始 HTTP 异常，也不自动重试。

Bark：将渠道改为 `bark`，在启动前设置：

```powershell
$env:BARK_DEVICE_KEY="你的设备密钥"
```

密钥不写进配置和采集文件。此处环境变量只是启动进程的本地凭据；时区内置默认为上海，
无需设置 TZ，也不需要把密钥发到聊天。

## 独立导入与统计

以下命令均在项目根目录执行，不启动浏览器、不会触发官网查价：

```powershell
.\work\collector-venv\Scripts\python.exe -X utf8 .\collector\monitor_passive.py --once
.\work\collector-venv\Scripts\python.exe -X utf8 .\collector\monitor_passive.py --report
```

从未安装项目的普通 Python 环境，先使用 `python -m pip install -e "./collector[dev]"`。
上述直接脚本入口无需可编辑安装；也可以从 `collector` 目录使用模块方式：

```powershell
..\work\collector-venv\Scripts\python.exe -X utf8 -m scripts.monitor_passive --once
..\work\collector-venv\Scripts\python.exe -X utf8 -m scripts.monitor_passive --report
```

`--once` 会按配置处理已有新事件，**若显式开启真实通知则也可能投递**；离线分析保持 console。
`--report` 只读统计（首次使用会初始化空库），不导入、不推送。

## 当前边界

已完成的是万豪真实响应的本地监控闭环，不是六集团全球全年无人值守爬虫。
IHG、Hyatt、GHA、雅高及自动酒店目录/全年查询调度没有因此自动就绪。
浏览器仍由用户正常操作；碰到官网403不重放、不换出口，不解除此前自动采集暂停。
通知传输使用模拟 HTTP 验证，尚未向用户手机真实发送。

## 本轮验收（2026-10-20）

完整246项测试通过，Ruff检查/格式及编译检查通过。本机独立测试Chrome的页面请求全部由本地样本拦截，
新增验证了两个独立、内容相同的房价请求都会保存，且事件ID不同；测试只关闭自己的临时Chrome。
离线覆盖历史/绝对阈值、每日最低价、确认/恢复/过期、事件冲突、跨重启24小时去重、
不确定投递、同酒店多方案合并、目录扫描和事务回滚。没有真实官网查价或手机推送。

实际本机 `--once` 已读取XIYWH旧文件并保存39条隔离样本；报告为0个新版事件、0条正式被动观测、
0个候选。没有把这份已知缺少事件标识的旧文件算作刚刚重新采集。
