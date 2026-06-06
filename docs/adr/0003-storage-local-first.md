# ADR-0003: 本地文件系统优先，MinIO 做远程 Worker 中转

## 背景

步骤间通过文件通信（JSON/MD/JPG/MP4/PDF）。需要选择文件存储方案。

## 选项

| 选项 | 优点 | 缺点 |
|------|------|------|
| 本地文件系统 | 零配置、直接读写、性能最好 | 多机需要共享方案 |
| MinIO (全量) | S3 兼容、多机透明访问 | 单机部署时多余 |
| NFS | 透明挂载、多机共享 | 远程 Worker 在内网，不能直连主机 |

## 决定

All-in-One 用本地文件系统 `/data/jobs/`。分层部署时加 MinIO 做远程 Worker 文件中转。

## 理由

1. 单机部署本地文件最简单
2. 远程 Worker 若不能直连主机 → 需要公网可达的文件中转
3. MinIO 部署在中转服务器，主机和远程 Worker 都出站连接
4. MinIO 文件 24h TTL 自动清理，不做持久存储
5. 主机上的 `/data/jobs/` 才是权威数据源

## 影响

- 所有 Worker 统一使用 `StorageBackend` 接口（pull/push），不区分本地/远程
- `LocalStorage`：数据在本机，pull/push 是 no-op
- `RemoteStorage`：通过 MinIO 拉取/推送，Worker 代码不变
- 不需要特殊的 Worker 子类——同一份代码，通过环境变量选择 backend
- 使用 MinIO 的 Worker 镜像需要加 `minio` Python 包
