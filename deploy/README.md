# deploy/ — Flori 部署配方

可分享、可复现的部署配置。**真实密钥/私钥永不入 git**(见仓库 `.gitignore`):
模板用 `${ENV}` 占位,真值放各机本地的 `deploy/edge/.env` 与 `deploy/tunnel/ssh/`。

## 目录
- `edge/` — 公网边缘机(如 ECS):Caddy(自签 TLS + Basic Auth 反代)+ 前端容器 + Watchtower;
  以及 flori worker(host 网络,经反向隧道连 NAS 的 redis/minio)。
  - `docker-compose.yml` / `worker.yml` / `Caddyfile` — 模板,全 `${ENV}` / `{$ENV}` 占位
  - `.env.example` — 复制成 `.env` 填真值(`.env` 已 gitignored)
- `tunnel/` — NAS → 边缘 反向 SSH 隧道(autossh):把 NAS 的 api/redis/minio 暴露到边缘回环。
  - `docker-compose.tunnel.yml` — 模板(`${EDGE_HOST}`、网络 `flori_default`)
  - `ssh/` — 放私钥 `id_ed25519` + `known_hosts`(gitignored,仅 `.gitkeep` 入 git)

## 单机生产(最简)
```bash
docker compose up -d        # 根 docker-compose.yml;数据走 .env 的 FLORI_DATA_DIR(留空=命名卷)
```

## 边缘 + 隧道(分布式)
```bash
cp deploy/edge/.env.example deploy/edge/.env     # 填 EDGE_HOST / MINIO_* / FLORI_BASIC_HASH
# 放 SSH 私钥到 deploy/tunnel/ssh/id_ed25519(本地,不入 git)
docker compose -f deploy/tunnel/docker-compose.tunnel.yml up -d           # NAS 侧起隧道
scp deploy/edge/* 边缘:/opt/flori-edge/ && ssh 边缘 'cd /opt/flori-edge && docker compose --env-file .env up -d'
# 前端镜像:git push → CI 建 ghcr 公开镜像 → 边缘 Watchtower(10s)自动 pull+重建,无手动推送。
# 根生产 compose 的 Watchtower 仍是 120s;edge 为了前端快速跟随 CI 单独设为 10s。
```
