# Pipeline 与 Runner 契约

状态：WP03 待冻结。

本文件将定义 GitLab CI YAML 严格子集、DAG 编译、重跑、Runner 注册/认领/续租、日志 sequence、secret inputs 和 Artifact commit。在 WP03 完成前，不得创建临时 DSL 或第二套 Runner 协议。

当前已冻结的上位边界见 [product.md](product.md) 和 [architecture.md](architecture.md)。
