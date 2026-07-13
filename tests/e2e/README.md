# 前端端到端冒烟（browser smoke）

无头 Chromium 跑通前端**全部路由**（含动态 `/content/{job}`、`/kb/{domain}/concepts/{term}`），
对每条路由校验：HTTP 状态、控制台 error、失败的 `/api` 请求，并截全页图。

这是对一个**已部署、在跑**的栈做的烟雾测试，和 `tests/`下的 hermetic 单测是两回事：
单测用 fakeredis、无网络无浏览器（`docker compose -f docker-compose.test.yml`）；本套件需要真前端 + 真 API。
因此脚本叫 `smoke.py` 而非 `test_*.py`（不被 pytest 收集）。脚本被显式调用时用退出码作门禁，
但当前不属于 `.github/workflows/ci.yml` 的每次 push / PR 必经 job。

## 跑

```bash
# 本机 LAN 前端（默认连 flori_default 网络上的 flori-fe-lan）
docker compose -f docker-compose.e2e.yml run --rm e2e

# 指向外网部署（带 Basic Auth）
BASE=https://你的域名 E2E_BASIC_USER=u E2E_BASIC_PASS=p \
  docker compose -f docker-compose.e2e.yml run --rm e2e
```

镜像 `docker/e2e.Dockerfile` 基于官方 Playwright 镜像（浏览器已内置），只补装对齐版本的
`playwright` python 包；首次会 build。

## 看什么

- 控制台打印 `N/M routes clean`；全干净退出 0，任一失败退出 1。
- 全页截图落在 `./output/e2e/*.png`（已 gitignore）。

## 改路由

路由表在 `smoke.py` 顶部 `routes`。动态 job/term 由 `/api/domains`、`/api/jobs`、
`/api/glossary` 实时解析（优先取有数据的领域），不硬编码测试数据。
