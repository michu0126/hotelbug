# Hotel Bug Price Monitor

自托管酒店价格情报平台，目标部署环境为群晖/Linux/Docker。雅高与 GHA 已通过官网页面的真实任务测试；六集团全球覆盖仍在开发。

## 当前交付与边界

- Python 3.12+ / FastAPI / SQLAlchemy / Alembic / PostgreSQL / Redis。
- 六服务 Compose、独立 Scheduler 与 Worker、持久化任务、优先级、去重、租约恢复、独立 Provider 限流。
- 标准化 Hotel/Rate 数据结构、Decimal 金额、追加式历史、报价身份隔离，并记录每次成功查询的有房/无房状态。
- Vue 3 + Vite 真实基础状态页；无演示报价。
- Marriott Worker 已切换为 Chromium 官网页面适配器：使用酒店名称搜索、从结果点击 View Rates，再选择日期并读取非会员含税费价。开发机全新浏览器会话仍在官网首页收到 403，NAS 实采尚未验收；启用开关不保证能够获取报价。旧的独立 JSON 适配器仅保留作研究代码，不再是 Worker 默认入口。
- 雅高浏览器采集器已接入 Worker：新加坡酒店 2027-09-28 的真实任务成功写入 4 条非会员含税报价。全球目录已发现 5,899 个雅高页面链接，尚未批量导入和验证。
- GHA 浏览器采集器已接入 Worker：有房日期可写入公开非会员报价，官网明确无房的日期标记为 UNAVAILABLE，不再把旧报价展示为当前可售。全球目录发现约 825 个页面链接，尚未批量验证。
- 希尔顿官网目录与浏览器适配器已接入 Worker：逐房型读取含税公开价，排除会员价，按真实房型/方案编号保存。官网索引筛出 542 个酒店子地图，目录任务已通过真实 XML 测试；独立浏览器报价仍返回 403，自动查价未通过，不能把开关已启用当作成功。
- 历史中位数降价判断、二次抓取确认、Telegram 重试队列和全年酒店轮询已实现；真实机器人送达、六集团全球数据、Linux浏览器/NAS实采仍待验收。
- [实施计划](IMPLEMENTATION_PLAN.md) / [Provider 研究记录](docs/providers/)。

## 覆盖旧项目与保留旧数据

仓库根目录的 Compose 现在是默认六服务版本，仍使用宿主端口 **8098**。
Docker Hub 的 `michu0126/hotelbug:latest` 是前端镜像；API 和 Scheduler 使用 `api-latest`，浏览器 Worker 使用 `worker-latest`。更新本次代码后需等待 CI 构建发布三个镜像，再更新群晖项目。
旧版的两个容器不能仅靠拉取 `latest` 升级：先在群晖 Container Manager 停止旧项目，再把项目 Compose 全文替换为本仓库根目录的 `docker-compose.yml`，配置新版本 `.env`，最后重新创建项目。不要同时运行两个占用8098的前端。

**不要删除旧版数据卷，也不要执行 `docker compose down -v`。**旧 Node/SQLite 数据不会自动导入 PostgreSQL；新数据库会从空库开始。旧版镜像固定在 `michu0126/hotelbug:sha-f34ff15`，回退请使用 [旧版 Compose](docker/compose.legacy.yml) 和原项目名/原数据卷；[旧版说明](docs/LEGACY_README.md) 仅供回退参考。

若需要与旧版并行验证，可将新项目的 `WEB_PORT` 设为 **8099**；但不能再沿用旧的两服务 Compose。

## 新版部署

Intel/AMD x86_64 群晖；当前只构建 amd64，ARM64尚未验证。独立 `worker` 构建目标包含 Chromium，API和Scheduler镜像不安装浏览器。浏览器采集建议 NAS 至少预留2GB内存，实际消耗以部署监测为准。

```bash
git clone https://github.com/michu0126/hotelbug.git
cd hotelbug
cp .env.example .env  # 仅首次安装；升级时保留已有 .env 并补充新变量
docker compose pull
docker compose up -d
```

首次部署前编辑 .env：
- POSTGRES_PASSWORD：改成随机的URL安全字符串（例如32字节随机十六进制）；不要在已有数据库上只改密码变量而不修改数据库角色密码。
- ADMIN_TOKEN：管理写入认证；留空时写入返回503，只读状态仍可用。
- WEB_PORT：默认8098；与仍在运行的旧版并行请改8099。

访问 `http://NAS-IP:8098`。只前端端口对宿主开放，PostgreSQL/Redis不映射宿主端口。
这是内网部署；公网访问需要带认证和TLS的反向代理。没有把登录界面冒充完整权限系统。

Compose 会按依赖健康顺序启动数据库、迁移/API、Worker、Scheduler、前端。根目录 Compose 使用镜像发布标签；部署前确认对应 CI 已成功发布。源码构建需在完整仓库目录中额外使用 `docker/compose.build.yml`：

```bash
docker compose -f docker-compose.yml -f docker/compose.build.yml up -d --build
```

## 配置

服务与采集配置从环境变量读取；Telegram Bot Token、Chat ID、启用状态也可在网页中用管理令牌保存，网页不会回显已保存的 Bot Token。该设置保存在 PostgreSQL 中，请保护数据库备份。
默认 TZ=Asia/Shanghai；数据库时间使用UTC。
`DATABASE_URL` / `REDIS_URL` 在Compose中按服务名生成，独立开发时可以自行配置。
并发、请求间隔、任务超时、调度周期见 .env.example。每个Provider有独立Redis锁/限流键，默认同一Provider并发1、间隔5秒。
最大任务重试3次、指数退避+jitter。403/挑战暂停6小时，429至少暂停1小时且不早于Retry-After。
任务租约超过执行超时60秒；崩溃任务可恢复，重复崩溃达到上限后终止。
Telegram 可在网页设置，也可使用 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID 环境变量。保存后可点击“发送测试消息”，立即核对 Bot Token、Chat ID 和当前网络出口；该按钮只发一条测试消息，不创建降价提醒，即使自动通知暂时关闭也可使用。有效报价入库后比较同一酒店、日期、房型、房价方案、币种和住客条件的历史每日中位数；至少 3 个历史观察日，默认降幅达到 50% 后排入复查，60 秒后重新采集仍满足条件才生成通知。发送失败持久化重试，429 遵守 Telegram retry_after。Bark/Webhook 尚未实现。

雅高和 GHA 采集分别启用 `ACCOR_ENABLED=true`、`GHA_ENABLED=true`。可选 `BROWSER_PROXY_URL=http://路由器地址:代理端口`；Telegram 默认使用同一出口，可用 `TELEGRAM_PROXY_URL` 单独指定。发送实现遵循 [Telegram Bot API](https://core.telegram.org/bots/api#sendmessage)。

希尔顿可用 `HILTON_ENABLED=true` 启用已注册的官网目录/页面适配器，但当前独立采集会话仍未获得报价访问，状态字段明确标为 `PUBLIC_PAGE_OBSERVED_WORKER_UNVERIFIED`。其目录候选按酒店主页逐一验证，不将 sitemap 页面数量当成已收录酒店数。万豪、IHG、凯悦及六集团全量价格验收仍未完成。

Worker 为每个集团保留独立浏览器会话，任务结束只关闭当前查询页，不再每次重建空白浏览器。会话自动保存在现有 `app-data` 卷的 `/data/browser-sessions`，无需新增环境变量；正常重启后仍保留网站状态，集团和代理出口之间隔离。会话文件可能包含网站 Cookie，请勿上传或公开。此修改不代表其他集团的报价访问已经恢复。

已启用且具有采集适配器的酒店默认进入全球目录轮询，每轮最多生成 20 个日期任务，未来 365 天滚动覆盖，活跃队列默认上限 2000。此处的“全球”指不限制酒店所在国家，不表示官方全球酒店目录或六集团房价适配已经全部验收。`GET /api/alerts` 可查看候选与复查结果；`GET /api/notifications` 需管理令牌，可查看发送结果。Telegram 在发送后进程崩溃、数据库尚未提交时可能重复投递，不能保证外部服务的严格一次发送。

Marriott 默认 `MARRIOTT_ENABLED=false`。启用后 Worker 使用新的 Chromium 会话操作官网页面，健康检查低频尝试 NYCMQ 未来单晚。403/挑战暂停6小时。用户手动浏览器能显示报价，但新的 Playwright 会话目前在官网首页返回 403；不能据此认定用户网络故障，也不能认为启用开关后就一定能自动获取报价。
单酒店 Watchlist 可选未来30/90/365天；所有已收录且已启用集团的酒店也会自动参与全年轮询。Scheduler 按 HOT/WARM/COLD 间隔去重，目录自动发现尚在开发。

## 增加酒店 / Provider

API文档 `/docs` 在API容器端口8000可访问；前端只反代/api，常用路由：
- GET /api/dashboard、/api/providers、/api/jobs
- GET /api/hotels?q=Shanghai&provider=marriott
- GET /api/hotels/{id}、/api/hotels/{id}/history?days=30
- GET /api/hotels/{id}/calendar?month=2026-10（真实报价日历；未查询显示 NO_DATA，官网成功查询但无房显示 UNAVAILABLE）
- POST /api/hotels（Authorization: Bearer ADMIN_TOKEN）
- POST /api/jobs（同认证，万豪 DISCOVER_HOTELS 可用 `payload.provider_hotel_id` 指定五位代码）
- POST /api/hotels/{id}/calendar/jobs?month=2026-10（同认证，为未来365天范围内该月逐日生成任务）
- GET/POST /api/watchlists（POST 请求 `{"hotel_id":"...","days_ahead":30}`，需管理令牌；仅单万豪酒店）

增加适配器必须先完善 docs/providers/<group>.md，再继承 HotelProvider。
只返回统一Pydantic模型，不向业务层泄漏原始JSON。注册到FACTORIES后才启用任务处理。
当前研究全部明确标注未知项，只有实际成功解析后才标 ONLINE；其他未实现 Provider 不启用。
HTTP错误工具在 crawler/http.py；浏览器试验只读取官网渲染页面，不复用登录 Cookie、不处理验证码。
配置、浏览器Session与Raw Fixture必须脱敏；单元测试使用本地fixture，不访问酒店网站。

## 开发与测试

```bash
cd backend
python -m venv .venv
# 激活当前系统的虚拟环境
pip install -r requirements-dev.txt
pytest -q
ruff check app tests alembic
ruff format --check app tests alembic
```

依赖版本锁定在 requirements.txt / requirements-dev.txt；前端使用 package-lock.json。
本地快速测试使用SQLite和fakeredis；CI还必须使用真实PostgreSQL和Redis，不能拿模拟测试替代。
集成测试会清空测试库，只允许名称以 `_test` 结尾的数据库及Redis DB15，严禁指向生产库。

```bash
cd frontend
npm ci
npm run build
```

## 数据库迁移

API启动自动执行Alembic升级，Worker/Scheduler不运行迁移。
修改模型时先生成并审查版本化迁移，不使用create_all代替生产迁移。

```bash
docker compose exec hotel-monitor-api alembic current
docker compose exec hotel-monitor-api alembic upgrade head
```

## 更新

```bash
docker compose pull
docker compose up -d
docker compose logs --tail=100 hotel-monitor-worker hotel-monitor-scheduler
```

切勿执行 `docker compose down -v`。升级前备份，破坏性迁移需要人工确认。
默认镜像已切换到新架构；旧版两服务部署必须按上面的覆盖步骤更新 Compose，不能仅执行镜像更新。

## 备份与恢复

在NAS的shell中将数据库导出到用户选择的备份目录，并同时安全备份.env：

```bash
docker compose exec -T postgres pg_dump -U hotelbug -d hotelbug -Fc > hotelbug.dump
```

恢复到**全新、空的数据库卷**，先仅启动postgres，数据库存在后导入，再启动其他服务：

```bash
docker compose up -d postgres
docker compose exec -T postgres pg_restore -U hotelbug -d hotelbug --no-owner < hotelbug.dump
docker compose up -d
```

不要对有数据的现有库直接恢复，避免重复或覆盖。Redis是队列投影，可以由PG到期任务恢复；通知与历史事实在PG中。
/data、/logs使用命名卷；业务结构化日志输出stdout，Docker每容器10MB×3轮转。
日志禁止记录Authorization、完整Cookie、密码和会话；默认不持久化原始响应。

## 阶段验收

Phase 0 已提交计划。Phase 1 本地22项测试、前端构建，以及CI Python3.12离线/真实PostgreSQL与Redis测试、迁移往返、六容器健康启动均通过。
[Phase 1 验收流水线](https://github.com/michu0126/hotelbug/actions/runs/36334647062)。Phase 2 仍有 NAS 官网独立 HTTP 验证、自动 Watchlist 等待完成，进度在 IMPLEMENTATION_PLAN.md 更新。
