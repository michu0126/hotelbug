# Hotel Bug Price Monitor — 实施计划

## 范围与阶段

最新需求文档为本次重构依据，覆盖此前“仅解析页面”和“仅 UI 配置”的限制。
先研究官网实际 JSON 请求，HTTP 页面其次，必要时才用浏览器；不猜测 Endpoint，不绕过访问控制。
本轮交付 Phase 0 + Phase 1，不同时实现六家 Provider，不声称基础框架已能查询全球房价。

| 阶段 | 范围 | 当前状态/验收 |
| --- | --- | --- |
| 0 | 架构、模型、队列、Provider 路线与迁移设计 | 已完成设计；旧版基线测试、构建已通过；本机无 Docker 引擎 |
| 1 | FastAPI / PostgreSQL / Redis / Alembic / Worker / Scheduler / 六服务 Compose | 完成：本地22项测试；CI离线22项及真实PG/Redis22项、迁移往返、六容器健康启动全部通过 |
| 2 | 只实现 Marriott，搜索/详情/房价/历史/日历 | 待开始；先验证官网有效日期搜索流程 |
| 3 | 中位数基线、评分、历史低价、二次确认、通知 | 待开始 |
| 4 | Vue 完整业务 Dashboard / 搜索 / 日历 / 趋势 / 配置 | 待开始；Phase 1 仅提供真实基础状态页 |
| 5 | Hilton / Hyatt / IHG 独立适配 | 待开始 |
| 6 | Accor / GHA 独立适配 | 待开始；旧版 GHA 样本仅作研究证据 |
| 7 | 缓存、优先级、公平性、压测、运行监控 | 待开始 |

## 架构与目录

Vue 3 + Vite → Nginx 同源 /api → FastAPI → PostgreSQL。
Scheduler 将到期持久化任务投递至 Redis 优先队列；Worker 独立执行 Provider。
PostgreSQL 是任务和报价的事实来源，Redis 是可重建的队列索引、锁、缓存、限流器。
每个 asyncio 任务独立 AsyncSession，不在并发任务间共享事务。

```text
backend/app/{api,core,database,models,schemas,providers,services,scheduler,crawler}
backend/alembic/versions       固定版本数据库迁移
backend/tests                 fixture/单元/集成测试
frontend/                     Vue 3 + Vite（不是原 Next.js）
docs/providers/               六集团证据、未知项和研究路线
docker/                       Nginx、保留的旧版部署配置
Dockerfile                    api / frontend 两个构建目标
docker-compose.yml            六服务部署
```

为避免和现有 Next.js `app/` 冲突，Python 包放在 `backend/app/`。
原 `app/`、`scripts/` 及 SQLite 卷不删除，旧 Dockerfile、Compose 和 README 保存在 docker/、docs/。

## 数据库 Schema

金额全部 Numeric/Decimal，时间存 UTC timestamptz，未知字段 NULL，币种不自动换算。

| 表 | 关键字段和约束 |
| --- | --- |
| hotels | UUID id；provider + provider_hotel_id 唯一；名称、品牌、国家/地区/城市、地址、经纬度、币种、官网、active、时间戳 |
| rates | 酒店、日期、房型/房价计划、金额、币种、积分、早餐/退款/会员/可订、成人/房间数、计价口径、抓取时间、来源、原始摘要；offer_key 唯一 |
| price_history | 每次有效观察追加；禁止覆盖历史；hotel/date/captured_at 和 offer_key/captured_at 索引；job_id + offer_key 唯一防重放 |
| price_statistics | offer_key 唯一；7/30/90 天中位数、历史高低、邻日中位数、同星期/同月份指标、样本数、时间戳 |
| price_alerts | offer_key、报价快照、基线、降幅、评分、等级、历史低价、confirmed、verification_job_id、通知去重键 |
| watchlists | scope=hotel/city/brand/country/provider；filters；enabled；HOT/WARM/COLD 配置 |
| crawl_jobs | kind/provider/hotel/date/priority/retry_count/scheduled_at/status；payload；lease_until；dedupe_key；未完成任务部分唯一索引 |
| provider_status | ONLINE/DEGRADED/BLOCKED/BROKEN；last_success/error；当天请求/成功/失败/平均延迟；blocked_until |
| notification_logs | alert/channel/status/attempts/dedupe_key/next_retry_at/sent_at；不存 token |
| settings | key、非敏感 JSON 值、版本/更新时间；部署和凭据以环境变量为准 |

价格比较身份包含 provider + hotel + 入住/退房 + 房型 + Rate Plan + 币种 + 成人/房间数 + 会员/早餐/退款状态 + 金额口径。
NULL 不是 false，未知房型或 Rate Plan 的最低价不能用于高置信 Bug 价通知。
原 SQLite 仅存最低可见价、且历史被覆盖，不能直接转成同房型历史基线。Phase 2 提供只读导入酒店/配置工具；旧报价可归档但不参与严格异常检测。

## Provider Interface

独立异步适配器：search_hotels(query)、get_hotel_details(id)、search_rates(request)、get_calendar_rates(request)、health_check()。
统一 Pydantic Hotel/Rate/SearchRequest/HealthResult；核心服务不能读取集团原始 JSON。
可选依赖只由需要的 Provider 导入。Phase 1 只定义接口/注册表，不发布六个返回假数据的实现。
错误统一：NETWORK_ERROR、TIMEOUT、HTTP_ERROR、PARSER_ERROR、INVALID_RESPONSE、RATE_LIMITED、BLOCKED_BY_ANTIBOT、TOKEN_EXPIRED、PROVIDER_CHANGED、NOT_IMPLEMENTED。
403/挑战中止，429 按 Retry-After 或保守退避；不复制会话到日志，不做验证码绕过。

## 队列与调度

任务类型：DISCOVER_HOTELS / FETCH_RATE / FETCH_CALENDAR / VERIFY_ANOMALY / PROVIDER_HEALTHCHECK。
先提交 PG 任务，再由 Scheduler 投影到 Redis ZSET；按 priority 排序，ID 去重。
Worker 弹出后在 PG 行锁下认领并设置租约，只有一个 Worker 能执行。
Worker 崩溃时，Scheduler 恢复过期租约；Redis 清空后从 PG 重建到期队列。
投递不是 exactly-once，要求业务持久化幂等：history(job_id,offer_key)、alert dedupe、通知记录。
Provider 并发令牌与最小请求间隔使用 Redis 原子 Lua；单任务超时小于租约与锁 TTL。
失败有限次数指数退避 + jitter；解析/权限错误不无限重试；某 Provider 被封锁不影响其他 Provider。
Scheduler 使用分布式领导锁，任务扫描分页，避免一次构造全酒店×365日任务。
Phase 1 验证持久化队列恢复、去重、限流与健康检查骨架；Watchlist 批次生产、波动权重和日期游标在 Phase 2/7 接入。

默认目标 HOT 0–30 天每3小时、WARM 31–90 天每9小时、COLD 91–365 天每48小时；目录每14天、健康检查每小时。
优先级：二次验证 > Watchlist > 近期/近期异常 > 波动酒店 > 普通远期；相同任务短期缓存。
缓存命中不能更新 captured_at，不冒充新抓取；空房是独立状态，失败不是0元。

## 异常检测与二次确认（Phase 3）

同一可比较报价序列先查 prior / median7 / median30 / median90 / historical_low。
邻日、同星期、当月均值和同房型历史只作为解释特征；不跨币种/计价口径比较。
默认至少3次独立历史观察才启用中位数基线；数据不足标记 INSUFFICIENT_HISTORY。
drop_percent=(baseline-current)/baseline*100；baseline优先median30，其次median7/90；baseline必须>0。
评分采用互斥降幅档（>20:+10、>30:+15、>40:+20、>50:+25、>70:+30），邻日:+15，接近历史低:+10或新低:+20（互斥），复核:+10。
注意：需求示例互斥累加最高75，达不到80档；Phase 3 显式提供可配置历史一致性支持项+15，样本充分且多个基线均异常时才加，避免暗中改成所有降幅档叠加。
score clamp 0–100：NORMAL<40、LOW<60、VERY_LOW<80、POSSIBLE_BUG>=80。权重/阈值均环境变量/设置配置。
第一次发现只保存 candidate；20–90秒 jitter 后 VERIFY_ANOMALY 绕过短期缓存、重新请求同条件，差异<=1%且仍满足异常则 confirmed。
恢复价、房型/Rate Plan/税费变化则拒绝确认，保留原因。
历史新低独立事件也进行确认；通知幂等键包含 offer identity、事件类型及价阶，仅再次显著下降允许重发。
Telegram/Bark/Webhook 优先；Email 后续。通知失败走持久化重试，不阻塞采集，不记录密码/headers。

## Docker / NAS / 运维

六服务：hotel-monitor-api、hotel-monitor-worker、hotel-monitor-scheduler、hotel-monitor-frontend、postgres、redis。
默认 WEB_PORT=8098；DB/Redis 不映射宿主端口；只前端对内网暴露。API 健康与依赖 readiness 分离。
镜像 Python3.12 slim，基础阶段不安装 Chromium，减少内存；Vue 编译产物由 Nginx 提供。
所有配置通过环境变量，时区默认 Asia/Shanghai，数据 /data、日志 /logs，标准输出结构化 JSON 并轮转。
Alembic 仅 API 启动时迁移，Worker/Scheduler 等待健康依赖，避免多进程同时迁移。
旧版 latest 保留；新版本使用 phase1-api / phase1-frontend 标签，基础框架未完成 Provider 前不覆盖稳定镜像。
amd64 必测；arm64 暂只作为设计兼容，不声称实测支持。
测试 Compose：启动依赖→迁移→API ready→投递 fixture 任务→worker处理→重启/Redis丢失恢复→容器健康。
开发机没有 Docker，真实 Docker 验证由 GitHub Actions 执行；没有成功记录前不标为通过。
备份用 pg_dump + 卷配置，恢复使用新库；升级不自动删除旧卷，不使用 down -v。

## 六集团研究路线

| 集团 | 已有证据 | 下一步；明确禁止猜接口 |
| --- | --- | --- |
| Marriott | 旧日期 URL 200但未建立日期搜索；/mi/query/phoenixShopAdvSearchInventoryDate仅预订日期上限 | 从真实搜索表单查有效报价请求，再做字段与会话研究；阻塞时标BLOCKED/DEGRADED |
| IHG | robots可读，旧redirect样本403 | 从集团官网实际搜索流程抓取请求；先证实日期、品牌、税费，不直接沿用旧URL |
| Hyatt | robots429，房价页403/E6020 | 暂停并记录；正常访问恢复后研究公开请求，无绕过 |
| Hilton | sitemap可读，房价页403 | 同上；不把目录可读视作房价可用 |
| Accor | 尚无实测 | Phase6先研究all.accor.com真实搜索，再实现 |
| GHA | 官网页面及其自行发出的 /api/v3/booking/hotel/rooms/rates 曾有成功样本 | Phase6验证真实请求上下文与更多品牌；否则沿用独立页面策略，不提前推广全量 |

研究细节保存 docs/providers；证据中的动态 Cookie/Authorization不保存。旧测试结果仅代表2026-09-27开发机，不代表NAS当前出口。

## Phase 1 验收记录（2026-09-28）

- 本地 Python 3.14：22项离线fixture测试、Ruff检查/格式检查通过；Vue生产构建通过。
- CI Python 3.12：离线fixture测试、真实PostgreSQL16/Redis7集成测试通过。
- Alembic upgrade → check → downgrade → upgrade 往返成功，模型与迁移一致。
- Compose六服务全部启动并健康，API readiness、dashboard、provider状态请求成功。
- 验证运行：https://github.com/michu0126/hotelbug/actions/runs/36334647062
- 测试不访问酒店官网；合成fixture只在测试使用，不进入生产镜像的数据或UI。
- 尚未完成项明确保留：Marriott实采、Watchlist任务生成、日历/趋势业务UI、异常分析/复核/通知，不能把Phase1视为最终采价产品。

### 技术资料

- https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html
- https://alembic.sqlalchemy.org/en/latest/cookbook.html#using-asyncio-with-alembic
- https://fastapi.tiangolo.com/deployment/docker/
- https://redis.io/docs/latest/develop/data-types/sorted-sets/
