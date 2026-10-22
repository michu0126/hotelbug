# 本机全球六集团监控

运行入口已扩展为 Marriott、IHG、Hilton、Hyatt、GHA、Accor。
官网全球目录自动续跑，收录后的酒店自动参加未来365天、1房2成人1晚的轮询。
队列默认最多120个待执行任务，按集团和酒店轮换；程序重启会保留目录及日期进度。

## 启动和查看

本项目根目录已有 Python 3.11 环境：

```powershell
cd E:\Codexspace\hotel-bug-watch
.\work\collector-venv\Scripts\python.exe -X utf8 -u .\collector\global_monitor.py
```

查看状态：

运行后打开本机页面：<http://127.0.0.1:8098/>。页面每10秒更新六集团的酒店、查询日期、
真实报价和暂停原因。页面端口在同一个配置文件内调整。

```powershell
.\work\collector-venv\Scripts\python.exe -X utf8 .\collector\global_monitor.py --status
```

停止自动任务并保留 Chrome：

```powershell
.\work\collector-venv\Scripts\python.exe -X utf8 .\collector\global_monitor.py --stop
```

单次限量验收可以使用 `--max-jobs 6`。任务包括目录和报价，成功价格条数在状态里单独统计。
遇到403、验证码或429，该集团暂停，其他集团继续。检查官网会话后显式恢复：

```powershell
.\work\collector-venv\Scripts\python.exe -X utf8 .\collector\global_monitor.py --resume-brand hyatt
```

不要同时运行旧的 `collector/automatic_monitor.py`；两者共享浏览器操作锁。
已安装环境的 `hotelbug-auto` 入口现指向全球程序；旧单酒店程序保留为 `hotelbug-marriott-auto`。

## 配置和数据

- `backend/local_global.toml`：六集团范围、浏览器端口/profile、请求间隔、队列上限、跌幅阈值。
- `data/global-monitor.sqlite`：酒店、官网目录游标、正常报价、查询结果、降价及确认记录。
- `data/global-queue.sqlite`：持久队列、集团限流和独占运行锁。
- 现有目录验收库只读导入酒店资料及目录进度；保留原采集时间与停用状态。
- 原万豪单酒店数据库和脱敏采集文件仍在原位置，人数不同的历史不合并。

浏览器使用本机普通 Chrome 的专用持久目录。启动时若端口已存在则连接已有浏览器；
程序仅操作自己创建的标签页，退出时保留用户的浏览器和标签页。
万豪中国站价格沿用已经验证的首页→酒店介绍页→选日期→查看房价，并观察真实响应。
目录提供日历链接时，先读取该页面公开的同酒店介绍页链接，再进入这一查价流程。
其他集团使用项目已有的官方页面采集器；每集团并发固定为1，任务之间随机等待。

首次进入全局库的报价需要重新积累历史。默认5个不同历史观察日、下降35%，
再等待180秒发起独立确认；确认结果进入原有通知待发送记录。此本机配置尚未填写
通知凭据，因此目前不会向手机推送。旧NAS的设置不会自动复制到本机。

## 覆盖口径

“全球六集团”是自动发现与调度范围。酒店资料数、真实报价数、成功查询日期数分别统计。
程序不会将官网目录公布的总数或排队数算作已经采完的报价。
全球全部酒店逐日查价仍取决于官网开放日期、访问状态和实际运行吞吐量；当前为单机串行采集。

新安装时，在 `collector/` 目录使用同一 Python 环境安装
`requirements-global.txt`。此入口要求项目的 `backend/` 与 `collector/` 同时存在。
这是本机采集入口；本次不改变群晖套件和已有容器。

2026-10-22 的真实全局队列验收已在万豪、IHG、GHA 三集团分别写入14、10、4条报价。
雅高首次任务遇到币种标题晚于房价渲染的问题，已修正并在原失败条件成功入库2条报价。
希尔顿HTTP403、凯悦HTTP429已独立暂停，其他集团继续。上述是本次验收样本数，
持续采集后的实际数量以本机页面为准。
