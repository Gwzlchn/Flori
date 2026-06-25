# ADR-0013: 版本号 = 单一语义版本 + 构建短 sha,所有组件共用

## 背景

早期各组件没有统一、可读的版本：worker 上报的是镜像 git hash（用户反馈「这个 hash 我都没意识到是版本号」），api/前端各自为政，无法一眼判断「线上跑的是哪一版、组件间是否漂移」。需要一个既可读（人能比较新旧）又可追溯（能定位到具体提交）的版本方案，且对单仓库多服务（api/scheduler/worker/前端一次 build 多服务用）友好。

## 选项

| 选项 | 优点 | 缺点 |
|------|------|------|
| 纯 git sha | 精确可追溯 | 不可读、无法比较新旧 |
| `git describe`（最近 tag + 距离 + sha） | 自动、可追溯 | 依赖打 tag 纪律；格式啰嗦 |
| 日期版本（CalVer） | 直观 | 不表达「改动大小」；同日多次发布需后缀 |
| **语义版本 + 构建短 sha** | 可读（比较新旧）+ 可追溯（定位提交）+ 单一来源 | 需约定递增规则并人工遵守 |

## 决定

**单一语义版本 + 构建短 sha**，所有组件共用同一个版本号。

- 版本唯一来源：`pyproject.toml` 的 `[project].version`（如 `0.3.0`）。
- 运行时 `FLORI_VERSION = "{semver}+{build_sha7}"`（如 `0.3.0+0dec508`）。`shared/version.py` 用 `importlib.metadata.version("flori")` 取语义版本，拼接环境变量 `FLORI_BUILD_SHA`[:7]；显式 `FLORI_VERSION` 可整体覆盖。
- 构建注入：`docker/base.Dockerfile` `ARG/ENV FLORI_BUILD_SHA`；CI（`.github/workflows/ci.yml`）以 `--build-arg FLORI_BUILD_SHA=${{ github.sha }}` 注入。
- **递增规则**（写进 `pyproject.toml` 注释 + 记忆，每次改动遵守）：只要有改动 **patch +1**；**逢 10 进位**（`0.2.9 → 0.3.0`、`0.9.9 → 1.0.0`）；**大重构 major +1**。系统或 worker 每次修改都要按此递增。
- 展示：前端从 `/api/status` 读 `version`，按 `+` 拆分「语义版本 / 构建 sha」两段显示；worker 卡灰副行常显短 sha，组件间漂移标警色，未上报显「版本未报」。

## 理由

1. 语义版本可读、能比较新旧；构建 sha 保留精确可追溯性——两者拼接兼得。
2. 单仓库一次 build 多服务，用同一个版本号最简单，也让「组件漂移」一眼可见。
3. 不依赖打 tag 纪律（CI 注入 sha 即可），比 `git describe` 轻。
4. 递增规则简单、可人工遵守，patch 逢 10 进位避免出现 `0.2.10` 这类双位 patch。

## 后果

- 发布前需手动按规则改 `pyproject.toml` 版本号（已在 CLAUDE/记忆中固化为习惯）。
- `FLORI_BUILD_SHA` 未注入时（本地裸跑）只显语义版本，不致报错。
- 相关：[[0014-observability-and-job-dag]]（版本号在 /system 健康总览页展示）。
