# 真实集成测试

`tests/integration/` 只放必须连接真实服务、真实 Docker daemon、独立进程或公网的测试，默认 unit 不收集。
唯一入口：

```bash
TEST_WARM_NAME=flori-test-my-task scripts/test.sh --integration
TEST_WARM_NAME=flori-test-my-task \
  FLORI_EXTERNAL_ARTICLE_URL=https://public.example/article \
  scripts/test.sh --external article
```

## 分层矩阵

| 层 | 允许的替身 | 当前责任 | 入口 |
|---|---|---|---|
| unit | fakeredis、单连接临时 SQLite、mock Docker/API | 纯函数、状态机、错误映射 | `scripts/test.sh -m ...` / `--all` |
| component | 进程内真实模块，外部边界可替身 | API、scheduler、worker 的模块接线 | unit 相关模块 |
| integration | 不替换 Redis、MinIO、SQLite 进程隔离与 Docker daemon | 双 Redis 客户端、Database 冷启动/多连接、迁移回滚、历史灾备恢复、Gateway Worker、real-docker、生产 AOF 与 MinIO 对象加载 | `scripts/test.sh --integration` |
| external | 不替换公网来源 | article 下载解析、audio 可播放下载、RSS 与 YouTube 枚举 | `scripts/test.sh --external <场景>` |

integration 栈使用唯一 compose project、固定 Redis、MinIO 与 child image、JUnit 工件、显式超时和退出清理。灾备回归直接调用生产备份/恢复入口：运行中的 appendonly Redis、生产 `Database` 和停写后的 MinIO 卷被恢复到全新目标。生产客户端逐字节复验 multipart 对象的 size、etag 与 user metadata。固定 format-v1 和 format-v2/schema-v2 归档持续验证历史兼容，不复制灾备或迁移实现。缺 Docker socket 或镜像是失败，不再在普通 unit 中无条件 skip。

检索质量门在同一入口内用 24 个冻结 job 和 96 条 query 生成
`$INTEGRATION_ARTIFACT_DIR/retrieval-quality.json`。24 个 job 必须逐一经过生产
`pipelines.yaml -> Scheduler.on_step_done -> completion effect` 摄入；测试禁止直调 DB
索引 helper。入口把当前完整 Git SHA 作为 `RETRIEVAL_QUALITY_MAIN_SHA` 注入容器，工件以
临时文件、`fsync` 和 `os.replace` 原子发布。两个独立 SQLite 的有序结果字节必须一致。

`decision_evidence_gate` 对样本数、指纹、过滤、已知词法修复和排序确定性 fail-closed；
只有它通过后，`quality_gate` 才能决定 FTS 是否满足预声明阈值。纯语义质量不足会形成可审计的
vector trigger，不会被伪装成测试基础设施失败；证据不足、未知 miss 或非语义回退仍令测试失败。
required CI 无论测试成功或失败都上传 JSON，缺文件直接失败。固定评测不访问公网或活 LLM。

外网入口只接受公开且不含凭证的 URL。`article`、`audio`、`rss`、`youtube` 分别由同名 `FLORI_EXTERNAL_*_URL` 提供；缺少所选 URL 时会打印 `SKIPPED` 原因并返回非零，不能把未执行记成通过。
如公网必须经过宿主代理，用 `FLORI_EXTERNAL_HTTP_PROXY` / `FLORI_EXTERNAL_HTTPS_PROXY` 指向容器可访问的地址，例如 `http://host.docker.internal:11081`；入口不会打印代理值。

三条 pipeline、两种 Document 体裁和四种来源形态的基本命中继续由搜索闭环回归承担；
24/96 黄金集负责可复算的质量决策。
本目录统一真实依赖、进程隔离、超时、清理和 CI gate，领域语义仍由对应测试模块持有。
