# configs/domain/ — 领域级 CPU 步骤调参

本目录的 `{domain}.yaml` 提供**按领域（domain）的 CPU 步骤参数微调**。

由 `shared/config.py:build_step_config` 加载（`load_domain_profile`），合并进传给步骤进程的
`config["domain"]`：

```python
"domain": {"name": domain, **domain_cfg}   # domain_cfg = configs/domain/{domain}.yaml
```

消费方（按 `config["domain"].get("<key>", {})` 读取，缺失即用代码默认值）：

| key | 步骤 | 可调参数 |
|-----|------|----------|
| `scene` | `step_01_scene` | `adaptive_threshold` / `min_scene_len_sec` / `window_width` / `min_content_val` |
| `dedup` | `step_03_dedup` | `phash_hash_size` / `phash_threshold` / `ssim_threshold` / `ssim_resize` |
| `ocr`   | `step_04_ocr`   | （预留） |

## 与 configs/prompts/profiles/ 的区别

两套配置层服务不同目的，**不要混淆**：

| 目录 | 作用 | 加载方 |
|------|------|--------|
| `configs/domain/{domain}.yaml` | CPU 步骤按领域调参（本目录） | `build_step_config` → `config["domain"]` |
| `configs/prompts/profiles/{domain}.yaml` | AI 笔记生成 profile（role/output_style/terminology） | smart 步骤从 `prompts_dir` 直接读 |

## 运行时路径

容器内 `CONFIG_DIR` 指向挂载的 `configs/`（dev/test/integration 为 `/app/configs`，
生产为 `/data/configs`）。`load_domain_profile` 查找 `${CONFIG_DIR}/domain/{domain}.yaml`，
不存在则返回 `{}`（沿用默认值）。
