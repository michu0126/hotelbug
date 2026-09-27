# RateDrop 全球酒店官网采集

面向万豪、IHG、希尔顿、凯悦和 GHA 的官网酒店目录同步与房价查询。**目前未实现或验证全球全量房价覆盖**。网页仅展示本机实际采集结果。

## 群晖部署

在 Container Manager 中使用仓库的 docker-compose.yml，无需填写环境变量。Compose 包含网页服务 ratedrop 和后台服务 monitor，共享 hotelbug-data 卷。现有用户需更新 Compose，只有拉取新镜像不会自动新增后台服务。

```bash
docker compose pull
docker compose up -d
```

打开 http://群晖IP:8098，在页面底部“监控设置”填写 Telegram Token、Chat ID 和监测参数，保存后可发送测试消息。时区内置 Asia/Shanghai，容器内部端口仍为3000。配置持久化在 /app/data/settings.json，后台下一批任务生效，无需重启。Token 不会通过读取接口回传；留空保留原值，清空 Chat ID 可停止通知。此页面无登录保护，请仅用于可信内网或通过带认证的反向代理访问。

当前镜像为 linux/amd64，适用于 x86_64 群晖；未发布 ARM 镜像。建议为浏览器预留至少 1 GB 内存。

## 实际行为与覆盖

- 从集团官网站点地图持续同步酒店，按酒店代码去重；GHA 还验证详情页类型。
- 酒店名录自动导入 SQLite 查询队列。五个集团均由后台自动尝试查询，万豪、IHG、希尔顿、凯悦仍可能遇到日期适配或访问拒绝，尚未验证稳定报价覆盖。
- GHA 通过酒店官网详情页的预订链接进入日期报价页，只解析浏览器实际显示的日期、成人数、币种和非会员未税起价，不直接调用报价接口。已加入真实酒店启动样例；其他 GHA 酒店仍需逐步验证，不支持的预订流程会记录错误。
- 默认目标为未来365天，每批最多30次查询。每个酒店轮流处理入住日期，后台每批结束后等待300秒继续；这不代表每天能够查完所有酒店的365天。
- 只有页面本身确认入住、离店日期，且明确显示币种和每晚价格，才保存报价。不仅依赖URL参数。
- 同酒店、同日期、同币种的最低可见每晚价下降35%以上时，将降价线索写入持久化推送队列。它不是同房型、同税费或同取消政策的保证，也不是已确认的错误价。
- 403、429或人机验证会暂停该集团6小时。目录普通连接失败5分钟后再试，报价普通页面识别失败1小时后再试。失败不会记录为零元。
- 网页展示目录数量、待同步文件数、实际检查次数、近24小时有效报价、错误与推送状态；不含演示酒店价格。
- 一次目录遍历结束仅表示站点地图已读取，无法证明官网未遗漏酒店或尚未营业的酒店已可预订。

2026-09-27 验证：GHA 的 NH Collection Dubai Ibn Battuta 官网预订页显示指定日期真实房价；页面采集器已纳入持久化自动队列。其他四集团首轮报价示例未通过：三个集团返回403，万豪未确认所请求日期。不能据此宣称已完成全量价格接入。GHA 非会员未税起价与其他采价口径分别建立基准，不跨口径比较。

## 配置与手动命令

以下参数均有内置默认值；除数据目录外可在 UI 修改。已保存的 UI 配置优先于旧环境变量。升级时保留原项目名称和数据卷，不要执行 docker compose down -v。

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| SCAN_DAYS | 365 | 目标日期范围，1–365 |
| SCAN_BATCH_SIZE | 30 | 每个批次最多查询次数 |
| CATALOG_PAGES_PER_GROUP | 25 | 每集团每批目录页面数 |
| MONITOR_INTERVAL_SECONDS | 300 | 两批之间的间隔 |
| SCRAPER_DELAY_MS | 5000 | 单次房价访问间隔，至少1500毫秒 |
| DROP_THRESHOLD | 35 | 降价提醒百分比 |
| SCRAPER_DATA_DIR | /app/data | 共享数据目录 |

```bash
# 单独续跑目录
docker exec hotelbug node scripts/sync-official-catalog.mjs
# 单独执行一个房价批次
docker exec hotelbug npm run scrape:rates
# 查看后台状态
docker compose logs --tail=100 monitor
```

目录检查点、酒店查询进度、报价与推送队列均持久化在共享卷。旧版 rate-history.json 保留但不导入为可信报价，新版会重新建立基准。Telegram未配置时降价消息保留待发送；发送失败最多重试10次，网络响应丢失时存在重复发送可能。

## 验证与发布

```bash
node --test scripts/monitor.test.mjs
npm run lint
npm run build:docker
```

推送 main 后 GitHub Actions 构建并发布 GHCR 和 Docker Hub 镜像。群晖采用新版 Compose 后后台持续运行，无需另外配置任务计划。

## 原始 Sites 工程说明

A clean full-stack starter running on [vinext](https://github.com/cloudflare/vinext), with optional Cloudflare D1 and Drizzle support.

## Prerequisites

- Node.js `>=22.13.0`
- Portable: Windows, macOS, or Linux; no Bash required
- Managed Linux: managed Linux runtime with Bash, `flock`, `curl`, `sha256sum`, and GNU `timeout`
- Git is required only for publishing

## Sites Lifecycle

The Sites initializer copies the shared starter and selects managed-linux only when `SITES_MANAGED_LINUX_CONTAINER=1`; otherwise it selects portable. It saves the selection only in ignored `.sites-runtime/execution-profile.json`. Both profiles copy/configure first, then use the plugin's separate `install-dependencies.mjs` step to measure installation independently. Edit source under `app/` and follow the Sites skill for installation, preview, builds, and publishing.

Whenever reopening or moving a checkout, run `node <plugin-root>/scripts/configure-execution-profile.mjs` before project commands. Profile changes do not alter tracked source or require reinstalling otherwise-valid dependencies; restart an existing preview to use the new selection. Do not commit or upload `.sites-runtime/`.

This starter does not use `wrangler.jsonc`.

`install:ci` runs `npm ci` once against the shared lockfile, disables parent-workspace discovery, and includes required dev/optional dependencies despite production/omit settings. Sharp defaults to prebuilt binaries unless explicitly configured otherwise. Do not overlap installers.

- **Portable:** Preserve host HOME, npm cache, registry, proxy, temporary paths, retry/concurrency settings, and lifecycle-script policy. Use `--prefer-offline --no-audit --no-fund`.
- **Managed Linux:** Use the existing project-local HOME/cache/tmp setup and Linux install lock, tarball preflight, and timeout. Restore the image-seeded npm cache only when its lockfile hash matches; retain network fallback. Builds keep their existing timeout. These helpers are not invoked by the portable profile.

`scripts/sites-env.mjs` preserves the caller's HOME, npm cache, proxy, XDG, and temporary-directory configuration while defaulting Wrangler and Miniflare state to the checkout. If npm reports an unwritable cache, select a writable path with `npm_config_cache` for that install. The `dev` and `start` scripts also keep Wrangler logs inside the checkout. Generated `.sites-runtime/` and `.wrangler/` directories are disposable and ignored by Git.

On portable, `npm run dev` uses `vinext dev` with HMR, starting at port 5173. Vinext records the running server in ignored `.vinext/` state, rejects an ordinary duplicate launch, and recovers stale state after a stopped process; exactly simultaneous starts can race. Pass `--port <port>` or `--hostname <host>` after `npm run dev --` when needed; keep portable previews on loopback.

On managed Linux, use `sites-preview start` only for requested browser QA. The project's dev script runs Vite and accepts the supervisor's `--host 0.0.0.0 --port 4173 --strictPort` arguments. The internal browser uses `http://terminal.local:4173/`; it is not a user-facing URL. The supervisor owns the preview lifecycle. The ignored local profile survives the supervisor's cleared process environment.

The portable profile simulates ChatGPT sign-in only for loopback development requests. Visit `/signin-with-chatgpt?return_to=/` to sign in as `local_seedy` (`seedy@sites.test`, display name `Seedy`) and `/signout-with-chatgpt?return_to=/` to sign out. The development cookie preserves that identity across server restarts. Mock auth is disabled in the managed-linux profile and is not included in production builds; hosted authentication remains dispatch-owned.

The Worker uses `vinext/server/fetch-handler`, including Vinext's config-aware image handling. After building, `npm start` runs that Worker locally through Wrangler on `127.0.0.1`, sharing `.wrangler/state` with dev preview and local D1 migrations; it does not deploy the site or simulate sign-in. Use the URL printed by the server. Pass `npm start -- --port <port>` to select a different built-preview port.

Local previews use Miniflare's placeholder `Request.cf` metadata without a network lookup. Set `CLOUDFLARE_CF_FETCH_ENABLED=true` to opt into fetching preview metadata; this setting does not change hosted request metadata.

Local tool usage metrics are disabled by default. Set `WRANGLER_SEND_METRICS=true` to opt in.

## Included Shape

- edit site code under `app/`
- `app/chatgpt-auth.ts` provides optional dispatch-owned ChatGPT sign-in helpers
- `.openai/hosting.json` declares optional Sites D1 and R2 bindings
- `vite.config.ts` simulates declared bindings for local development
- `db/index.ts` reads the D1 binding from the Cloudflare Worker environment
- `db/schema.ts` starts intentionally empty
- `@cloudflare/workers-types` provides Worker types; `cloudflare-env.d.ts` declares optional `DB`/`BUCKET` bindings—update these declarations if binding names change
- `examples/d1/` contains an optional D1 example surface
- `drizzle.config.ts` supports local migration generation when needed

## Workspace Auth Headers

Signed-in visitors receive both `oai-authenticated-user-id` and `oai-authenticated-user-email`. Private Sites require every visitor to sign in; public Sites may also have anonymous visitors, for whom neither header is present.

The user ID is stable for the same user on the same Site and different across Sites. Use it as the durable user key; use email and name for display or contact purposes.

SIWC-authenticated workspace sites may also receive `oai-authenticated-user-full-name` when the user's SIWC profile has a non-empty `name` claim. The full-name value is percent-encoded UTF-8 and is accompanied by `oai-authenticated-user-full-name-encoding: percent-encoded-utf-8`.

Treat the full name as optional and fall back to email when it is absent:

```tsx
import { headers } from "next/headers";

export default async function Home() {
  const requestHeaders = await headers();
  const userId = requestHeaders.get("oai-authenticated-user-id");
  const email = requestHeaders.get("oai-authenticated-user-email");
  const encodedFullName = requestHeaders.get("oai-authenticated-user-full-name");
  const fullName =
    encodedFullName &&
    requestHeaders.get("oai-authenticated-user-full-name-encoding") ===
      "percent-encoded-utf-8"
      ? decodeURIComponent(encodedFullName)
      : null;

  const displayName = fullName ?? email;
  // ...
}
```

## Optional Dispatch-Owned ChatGPT Sign-In

Import the ready-to-use helpers from `app/chatgpt-auth.ts` when the site needs optional or required ChatGPT sign-in:

- Use `getChatGPTUser()` for optional signed-in UI.
- Use the returned `userId` as the stable user key for user-owned records; do not use email as a durable identifier.
- Use `requireChatGPTUser(returnTo)` for server-rendered pages that should send anonymous visitors through Sign in with ChatGPT.
- In a Server Component, start sign-in with `<a href={chatGPTSignInPath(returnTo)} target="_top">`. The auth helper module is server-only; do not import it into a Client Component.
- Do not use `fetch`, XHR, a client-side router, or a framework link that can prefetch the sign-in route. SIWC must start as a top-level navigation.
- Never request the AuthAPI authorization endpoint directly. The dispatch-owned `/signin-with-chatgpt` route must start the SIWC flow.
- Use `chatGPTSignOutPath(returnTo)` for browser sign-out links or actions.
- Pass a same-origin relative `returnTo` path for the destination after sign-in or sign-out. The helper validates and safely encodes it.
- Mark protected pages with `export const dynamic = "force-dynamic"` because they depend on per-request identity headers.

Dispatch owns `/signin-with-chatgpt`, `/signout-with-chatgpt`, `/callback`, the OAuth cookies, and identity header injection. Do not implement app routes for those reserved paths. Routes that do not import and call the helper remain anonymous-compatible.

SIWC establishes identity only; it does not prove workspace membership. Use the Sites hosting platform's access policy controls for workspace-wide restrictions, or enforce explicit server-side membership or allowlist checks.

Use SIWC for account pages, user-specific dashboards, saved records, and write actions tied to the current ChatGPT user. Leave public content anonymous.

## Local D1 migrations

For a D1-backed local preview, generate SQL with `npm run db:generate`. Build once through the Sites skill's build entrypoint (or `npm run build` for standalone use) to generate `dist/server/wrangler.json`, rebuilding if bindings change. From the project root, apply each pending migration in order:

```sh
node --import ./scripts/sites-env.mjs ./node_modules/wrangler/bin/wrangler.js d1 execute DB --local --config dist/server/wrangler.json --persist-to .wrangler/state --file drizzle/0000_example.sql
```

Replace the filename with the pending migration and `DB` with your D1 binding name if different. Use `.wrangler/state`, not `.wrangler/state/v3`; Wrangler adds the versioned directories. Do not replay migrations already applied locally. This updates only the preview database; publishing applies production migrations separately.

## Diagnostic Commands

- `npm run install:ci`: perform the one locked dependency install
- `npm run dev`: start the Vite/Vinext development server
- `npm run build`: build the deployable Sites artifact
- `npm run start`: preview the built Worker locally with D1/R2 support
- `npm run db:generate`: generate Drizzle migrations after schema changes

When using the Sites plugin, follow its skill instructions for installation, builds, and publishing. These npm commands remain available for standalone use.

The portable build runs Vinext directly without a host `timeout` command. The managed-linux build uses `scripts/build-verified.sh` and its existing `SITES_BUILD_TIMEOUT` setting.

## Learn More

- [vinext Documentation](https://github.com/cloudflare/vinext)
- [Drizzle D1 Guide](https://orm.drizzle.team/docs/get-started/d1-new)
