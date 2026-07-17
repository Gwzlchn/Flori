# Audio 处理步骤

音频与播客保持独立 `content_type=audio` 和 `pipeline=audio`：

```
01_download → 02_whisper → 03_transcript_parse → 04_smart_podcast → 05_review
```

下载、转写、时间段 locator、智能笔记和评审继续复用 StepBase、Worker、Prompt 与 canonical evidence
基础设施。音频不属于 Document Model；它的原始媒介和稳定定位仍是媒体文件与毫秒区间。
