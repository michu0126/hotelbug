# 群晖简化页面与采集分流发布验收（2026-10-18）

- GitHub 发布提交：`cce4c964e951a0cd97109e424aa1232806300a5c`。
- 云端验证与三个镜像发布均成功：
  https://github.com/michu0126/hotelbug/actions/runs/39054347566
- NAS 8098 页面已实机打开，包含概览、价格日历、降价通知、设置四个入口。
- API、Frontend、Worker、Scheduler 使用该提交固定镜像标签，全部健康。
- Postgres、Redis 容器保留原实例，未重启 Container Manager。
- 发布前完整 pg_dump 已通过 pg_restore 解析检查，保存于
  `/volume1/docker/hotel/release-backup-cce4c96-20261018`。
- 六集团私有 Cookie 的 revision 均与发布前一致，未改写、清空或重新导入。
- 发布后 947 家酒店，统计日报价 709 条，Worker/Scheduler 心跳恢复；这些数值
  不是全球全年覆盖或本次新采集条数。
- 容器内 registry / marriott_hybrid / collection_strategy 与本地发布源码 SHA256 一致。

旧 Worker 停止后验证无存活 Worker，仅将其持有的凯悦三个 Chromium IPC symlink
移至 `/data/profile-lock-backup-cce4c96-20261018`，未删除 Cookie、数据库或浏览器资料。

## 验证边界

万豪英文接口通过项目浏览器 request context 发送，使用其 Cookie jar；中文站继续
页面采集。其余五集团没有已接入可用报价接口，继续 Cookie 浏览器采集。
本地真实 Chromium 的模拟接口验证通过，零酒店请求、零 Telegram 请求。
真实英文接口报价仍未在 NAS 验收，页面明确提示，不以模拟测试冒充真实报价。
更新后 IHG、GHA ONLINE；万豪/希尔顿受官网访问限制，凯悦 RATE_LIMITED，雅高 TIMEOUT。
没有重置这些状态或宣称全球全年采集完成。

实机截图：`work/nas-simple-dashboard-20261018.png`。
