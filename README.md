# Hotel Bug Price Monitor

自托管酒店价格情报平台，目标部署环境为群晖/Linux/Docker。当前为 **Phase 2 万豪实验预览版**，不是六集团报价已接入的完成版。

## 当前交付与边界

- Python 3.12+ / FastAPI / SQLAlchemy / Alembic / PostgreSQL / Redis。
- 六服务 Compose、独立 Scheduler 与 Worker、持久化任务、优先级、去重、租约恢复、独立 Provider 限流。
- 标准化 Hotel/Rate 数据结构、Decimal 金额、10 张数据库表、追加式历史、报价身份隔离。
- Vue 3 + Vite 真实基础状态页；无演示报价。
- Marriott 官网浏览器中实测了 NYCMQ 的有效日期、酒店详情与55条房型报价；增加了官方 GraphQL 解析适配器、同报价历史日历、手动按月排队及单酒店自动监控。**独立 HTTP 测试未成功，NAS 实采仍未验收，默认关闭。**
- 异常评分/二次验证/通知发送、完整趋势/多范围 Watchlist UI 属于后续阶段，基础表不等于功能已完成。
- [实施计划](IMPLEMENTATION_PLAN.md) / [Provider 研究记录](docs/providers/)。

## 旧版用户：不要直接覆盖正在运行的部署

原 Node/SQLite 采集器完整保留，原稳定镜像 `michu0126/hotelbug:latest` 不由新流水线覆盖。
旧 Compose 在 `docker/compose.legacy.yml`，旧镜像构建定义在 `docker/legacy.Dockerfile`，旧说明在 [LEGACY_README](docs/LEGACY_README.md)。
旧数据卷不删除、不自动转换。最低价快照不具备严格同房型/Rate Plan 历史，不能直接导入异常基线。

新版使用独立 Compose 项目名 `hotelbug-v2`、独立卷和 `phase2-preview-api` / `phase2-preview-frontend` 镜像标签。
并行验证时将 WEB_PORT 设为 **8099**，避免占用旧版8098。旧版可以继续运行。
恢复旧版时沿用原项目名、原卷和原Compose；不要用新项目名挂载空卷误以为数据丢失。

## 新版部署

Intel/AMD x86_64 群晖；当前只发布 amd64，ARM64尚未验证。镜像无 Chromium；万豪适配器直接请求官网观察到的 JSON 端点。
建议 NAS 至少预留1GB内存，实际消耗以部署监测为准。

```bash
git clone https://github.com/michu0126/hotelbug.git
cd hotelbug
cp .env.example .env
docker compose up -d
```

首次部署前编辑 .env：
- POSTGRES_PASSWORD：改成随机的URL安全字符串（例如32字节随机十六进制）；不要在已有数据库上只改密码变量而不修改数据库角色密码。
- ADMIN_TOKEN：管理写入认证；留空时写入返回503，只读状态仍可用。
- WEB_PORT：默认8098；与旧版并行请改8099。

访问 `http://NAS-IP:8098`。只前端端口对宿主开放，PostgreSQL/Redis不映射宿主端口。
这是内网部署；公网访问需要带认证和TLS的反向代理。没有把登录界面冒充完整权限系统。

Compose 会按依赖健康顺序启动数据库、迁移/API、Worker、Scheduler、前端。
若尚未拉到阶段镜像，可在仓库中执行 `docker compose up -d --build` 从源码构建。

## 配置

所有部署配置从环境变量读取；API返回不会泄露数据库URL、Token或Cookie。
默认 TZ=Asia/Shanghai；数据库时间使用UTC。
`DATABASE_URL` / `REDIS_URL` 在Compose中按服务名生成，独立开发时可以自行配置。
并发、请求间隔、任务超时、调度周期见 .env.example。每个Provider有独立Redis锁/限流键，默认同一Provider并发1、间隔5秒。
最大任务重试3次、指数退避+jitter。403/挑战暂停6小时，429至少暂停1小时且不早于Retry-After。
任务租约超过执行超时60秒；崩溃任务可恢复，重复崩溃达到上限后终止。
通知变量 TELEGRAM_BOT_TOKEN/CHAT_ID、BARK_URL、WEBHOOK_URL 已预留；**当前不发送通知**，适配器在Phase3实现。

Marriott 默认 `MARRIOTT_ENABLED=false`。在 NAS 上启用前需确认网络出口可访问官网；启用后健康检查按小时低频尝试 NYCMQ 未来单晚。403/挑战会暂停6小时。浏览器能显示报价并不能证明群晖独立 HTTP 请求能成功。
单酒店 Watchlist 可选未来30/90/365天，Scheduler 轮转日期且按 HOT/WARM/COLD 间隔去重；不会每次扫描全球酒店×365天。要自动采集须先启用 Provider、添加酒店代码并保存自动监控。多酒店/城市/国家批量监控尚未实现。

## 增加酒店 / Provider

API文档 `/docs` 在API容器端口8000可访问；前端只反代/api，常用路由：
- GET /api/dashboard、/api/providers、/api/jobs
- GET /api/hotels?q=Shanghai&provider=marriott
- GET /api/hotels/{id}、/api/hotels/{id}/history?days=30
- GET /api/hotels/{id}/calendar?month=2026-10（真实报价日历，缺失日期显示 NO_DATA）
- POST /api/hotels（Authorization: Bearer ADMIN_TOKEN）
- POST /api/jobs（同认证，万豪 DISCOVER_HOTELS 可用 `payload.provider_hotel_id` 指定五位代码）
- POST /api/hotels/{id}/calendar/jobs?month=2026-10（同认证，为未来365天范围内该月逐日生成任务）
- GET/POST /api/watchlists（POST 请求 `{"hotel_id":"...","days_ahead":30}`，需管理令牌；仅单万豪酒店）

增加适配器必须先完善 docs/providers/<group>.md，再继承 HotelProvider。
只返回统一Pydantic模型，不向业务层泄漏原始JSON。注册到FACTORIES后才启用任务处理。
当前研究全部明确标注未知项，只有实际成功解析后才标 ONLINE；其他未实现 Provider 不启用。
HTTP错误工具在 crawler/http.py；无需浏览器的Provider不得引入浏览器。
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
新版预览镜像与旧 latest、Phase1 镜像标签分开，不自动切换旧版用户。

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
