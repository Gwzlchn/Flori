# vNext 黄金样本

这里是 vNext 唯一的验收样本入口。样本刻意很小，目标是证明保留功能的结构和不变量，不复刻旧 Python 测试数量。

## 离线样本

| 输入 | 用途 | 必须结果 |
|---|---|---|
| `digital-paper.pdf` | 数字版论文 | 有文本层；抽出章节、Figure、Table 页面区域和 PDF evidence |
| `scanned-paper.pdf` | 扫描版 PDF | 检测阶段返回 `unsupported_scanned_pdf`，不得进入 extractor |
| `local-video.mp4` | 三秒本地视频 | 识别 3000 ms 时长；生成字幕、关键帧、机械笔记和智能笔记 |
| `local-video.srt` | 固定字幕 | 标准化为三个连续时间段，所有时间引用落在视频范围内 |
| `keyframe-1000ms.jpg` | 固定关键帧 | 与 1000 ms 引用绑定，摘要匹配 manifest |

`expected/` 是结构验收样例，不是第二套产品 DTO。每个 JSON 都带精确 `flori.*.v1` schema；`flori-core` 的 `golden_contracts` 测试直接反序列化并验证这些文件，fixture 不定义任何字段。

## 验收原则

- AI 文本不做逐字 golden。只检查正式 schema、必填章节、机械/智能边界和引用完整性。
- Figure 保留 caption 与 PDF 页坐标；Table 只保留页面区域和普通文本，不解析单元格模型。
- 机械笔记只能重组字幕事实。智能笔记必须把 AI 分析与来源事实分开。
- canonical evidence 必须能定位到 PDF 页坐标或视频毫秒区间和关键帧。
- 智能笔记和摘要只用 `[[evidence:<UUIDv7>]]` 引用候选；Rust 校验通过后才生成 canonical evidence。
- 搜索只验可重建索引的命中、Source/Job 归属和 evidence，不固定排序分数。
- 所有离线二进制都是本项目生成的 CC0 合成样本，不含生产 Artifact、账号、cookie 或私有内容。

## 外网样本

`manifest.yaml` 只冻结 arXiv、PDF、Bilibili、YouTube 和频道订阅标识。它们不在普通 CI 下载；只有显式 external 测试、网络可用且凭据边界满足时才运行。外站内容的许可和可下载性必须在执行时重新确认。

## 当前可执行的基线检查

```text
(cd tests/fixtures/vnext && sha256sum -c SHA256SUMS)
pdftotext tests/fixtures/vnext/digital-paper.pdf -
pdftotext tests/fixtures/vnext/scanned-paper.pdf -
ffprobe tests/fixtures/vnext/local-video.mp4
FLORI_RUNNER_MEDIA_IMAGE=flori-runner-media:local cargo test -p flori-runner --test video_image -- --nocapture
```

第二个 `pdftotext` 去掉空白和换页符后必须为 0 字节。WP04 把这些检查收进 `cargo xtask`，此前不增加临时测试脚本。
