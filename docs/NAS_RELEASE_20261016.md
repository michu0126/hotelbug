# 群晖发布与运行检查：2026-10-16

用户本轮明确要求先推送，再从实际容器找问题，取代此前暂不推送的要求。

## 已完成的发布准备

- GitHub `main` 已推送 `70f74f57ac50b86d08dba449791befb4ded2356c`，随后修复
  PostgreSQL 测试夹具的外键写入顺序并推送 `e3c6c6b5efad63b5c077b2edb252232ef9296285`。
- 最新流水线：<https://github.com/michu0126/hotelbug/actions/runs/38766126501>。
  离线1975项、PostgreSQL／Redis集成1975项、迁移往返与六服务容器验证均成功。
  API、Worker、前端镜像已成功发布到 Docker Hub 与 GHCR。
- 增加 GHCR 按完整提交号发布的标签，群晖候选配置固定版本，避免拉取到漂移的 latest。

## 实际容器初始状态

SSH 连接用户原群晖 `192.168.50.13`，只操作 Compose 项目 `hotelbug`。
最初六个容器均已退出约5天，8098直接拒绝连接；PostgreSQL旧退出码137，
Redis与前端退出码0，API／Scheduler为143，Worker为0。未证明停止的具体操作来源。

原有库：747家酒店、24,614条历史报价；已启用六集团与全球监控。
旧迁移版本 `0003_recheck_indexes`，万豪未采纳任何明确目录来源、暂无万豪／凯悦酒店。
雅高73、GHA388、希尔顿5、IHG281。保存的集团暂停均为10月10日，已自然到期。
未清空任务、历史、浏览器资料、暂停或 Telegram 设置。

数据库和Redis启动后健康；为恢复网页访问，启动原API与前端，
`/api/health/ready`返回ready，旧页面已可访问。Worker与Scheduler暂未启动，
等待统一切换新版；原页面此时不代表新版已部署。

## 备份和配置

在 `/volume1/docker/hotel/release-backup-70f74f5-20261015/` 保存原 Compose、
`.env` 与 PostgreSQL custom-format备份 `hotelbug-before-migration.dump`。
备份为7,304,626字节，已用 `pg_restore -f /dev/null`完整读取校验，无写入原库。
目录在跨午夜前创建，因此保留10月15日后缀。

候选 `compose.release-e3c6c6b.yaml`保留端口8098、原数据库卷、浏览器数据卷、代理
和环境设置，移除旧的逐文件运行补丁挂载，防止覆盖新版完整模块。
新配置已通过容器侧 Compose校验。NAS访问GHCR与Docker Hub注册表均返回正常401认证响应。

全过程未停止、重启或更新 Container Manager 套件，未操作其他项目容器。

## 2026-10-17继续部署

已再次确认三个发布任务均成功，使用同一完整提交号固定镜像。三个镜像已拉取，
平台均为 linux/amd64，使用 `--pull never` 重建应用，未重建数据库或Redis。

当前发布前另外创建 `/volume1/docker/hotel/release-backup-e3c6c6b-20261017/`，
保存当前Compose、`.env`和7,304,626字节数据库备份，完整读取校验通过。

新版API与前端已健康，8098的 `/api/health/ready` 实测HTTP 200、`ready`。
迁移已到 `0004_calendar_hints`；747家酒店、24,614条历史报价保留。
`/api/catalogs`已返回未来365天报价覆盖和独立月历线索覆盖字段；尚未覆盖的日期不冒充成功。
容器内五个关键模块SHA256与发布源码一致，旧逐文件补丁挂载已从应用配置移除。
运行时六集团和全球监控启用，浏览器代理为原配置 `http://192.168.50.4:7890`。

通过正常管理API提交万豪中文全球目录任务：
`2b38e6fa-2297-485b-af18-f43fd20f2da5`，HTTP200、初始PENDING。
仅请求新目录来源，不预先标记采纳成功或清除集团暂停。
Worker与Scheduler正在按相同版本重建，完整启动日志保存在
`/volume1/docker/hotel/nas_collectors_deploy_20261017.log`。

## 待实际完成

Worker／Scheduler健康、万豪中文官方目录实际完成、自动月历任务、
新版浏览器和新价格入库需继续从容器核验；提交任务不等于已成功采集。

## 容器内实际定位与修复

1. 万豪首个目录任务报 `BROWSER_UNAVAILABLE`，原因为资料锁仍指向已删除的
   旧Worker `341a0c5866b1`。IHG同样遗留旧锁。已检查当前Worker进程与共享卷消费者，
   再仅停止采集容器，确认 `exited false` 后移动两份资料内的
   `SingletonLock/SingletonCookie/SingletonSocket` 六个符号链接，未移动其他文件。
   可恢复备份位于应用数据卷 `/data/profile-lock-backup-20261017`；资料与Cookie保留。
   采集容器已恢复，同一官方目录重新提交任务
   `acf5d855-6735-4226-abb0-3572bb73ffb0`，未清除任何集团暂停。
2. Worker／Scheduler有真实Redis心跳，但Docker的5秒检查反复超时。
   容器实测仅导入旧检查模块就耗时9.114秒；NAS当时系统负载约39至41。
   改为标准库直接验证Redis实际心跳，避免每轮加载应用、Pydantic和Redis客户端。
   保留认证、数据库选择、TLS验证、三秒总预算与错误失败行为，未放宽心跳有效期。
   新检查在实际Scheduler容器执行成功；已无重启替换两容器内检查模块作为现场修复，
   待统一发布镜像固化。新增18项测试通过，连同关闭／调度回归共28项通过。

尚未取得新报价入库证据；真实TLS／限流等提供方问题与本地启动故障分别记录。
