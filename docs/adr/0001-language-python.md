# ADR-0001: Python 3.11+ 作为主语言

## 背景

需要选定后端和步骤脚本的开发语言。

## 选项

| 选项 | 优点 | 缺点 |
|------|------|------|
| Python | 科学计算生态最好(OpenCV/Whisper/OCR)、Claude 擅长、原型已用 Python | 性能不如编译语言 |
| Go | 性能好、并发模型优秀 | 科学计算库弱、原型代码需重写 |
| TypeScript | 前后端统一 | 科学计算库弱 |

## 决定

Python 3.11+。

## 理由

1. 原型 `steps/*.py` 已验证可行，迁移成本最低
2. OpenCV、PySceneDetect、RapidOCR、Whisper 等全是 Python 库
3. FastAPI 异步性能足够（个人工具，非高并发）
4. Claude Code 对 Python 代码生成质量最好
5. CPU 密集步骤（场景检测/OCR）瓶颈在 C 扩展，Python 只是胶水

## 影响

所有后端组件（API/调度器/Worker/步骤）统一用 Python。前端用 Vue 3 (JavaScript)。
