# 测试指南

## 现状判断

现有测试已经覆盖 FFmpeg 去重命令、OCR/ASR 路由、字幕时间轴、翻译与审核
契约、Agent 检查点、解说时间轴、TTS、响度、可解码成片、Web 分片上传、
访问密钥隔离和路径越界。它们不是单纯追求数量。

此前的主要缺口位于系统边界。本轮补充：

- 逐视频输出检查点的源文件和配置签名校验；
- 服务异常退出后进入 `paused`，原任务断点续做；
- 字幕 → 去重组合流程的模块顺序与阶段检查点；
- 不同访问密钥的服务器存储命名空间隔离；
- 用户只能统计、预估和清理自己的服务器资源；
- 环境预检 API；
- 解说渲染中终止 TTS/FFmpeg 子进程树。
- 远程渲染配置字段白名单与 LLM 内网地址拦截；
- 单文件、单任务、账户预留容量、上传速率和磁盘余量；
- Agent 能力挑战、最终修订制品读取证据和主机路径脱敏；
- 声纹参考音频变化时 TTS 缓存自动失效；
- 异常恢复只终止 PID、启动时间和可执行文件均匹配的进程。

## 测试层级

| 层级 | 目标 | 主要文件 |
|---|---|---|
| 纯单元 | 文本、配置、命令、路径、状态转换 | `test_video_dedup.py`, `test_subtitle_tool.py`, `test_batch_pipeline.py` |
| 契约 | Agent 请求/响应、检查点、质量门槛 | `test_agent_bridge.py` |
| 服务集成 | API、数据库、权限、上传、恢复、清理 | `test_web_gateway.py` |
| 媒体集成 | 真实 FFmpeg 生成、拼接、解码、响度 | `test_recap.py` |
| 并发/跨进程 | 全局 ASR/LLM 槽位 | `test_global_slots.py` |

## 标准命令

```powershell
cd E:\wangyang\Documents\Codexfile\climind\video-dedup-local
.\.venv-ocr\Scripts\python.exe -X utf8 -m unittest `
  test_video_dedup test_subtitle_tool test_recap test_agent_bridge `
  test_global_slots test_batch_pipeline test_web_gateway
```

前端静态语法检查：

```powershell
Get-ChildItem web_gateway\static\js\*.js |
  ForEach-Object { node --check $_.FullName }
node --check web_gateway\static\app.js
```

## 覆盖率

覆盖率只作为诊断指标，不代替媒体可解码和权限边界测试：

```powershell
.\.venv-ocr\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv-ocr\Scripts\python.exe -m coverage run -m unittest `
  test_video_dedup test_subtitle_tool test_recap test_agent_bridge `
  test_global_slots test_batch_pipeline test_web_gateway
.\.venv-ocr\Scripts\python.exe -m coverage report -m
```

重点关注 `web_gateway/worker.py`、`web_gateway/storage.py`、
`web_gateway/recap_service.py` 和 `batch_pipeline.py` 的分支覆盖。OCR/TTS
第三方库内部不计入本项目覆盖率目标。

## 发布前组合流程

1. 仅去重：上传 → 去重 → 发布；
2. 仅字幕：上传 → OCR/ASR → Agent/API → 字幕终稿；
3. 字幕 + 去重：字幕检查点 → 去重/烧录检查点 → 发布；
4. 仅解说：视频和已有字幕 → Agent 编排 → 预览 → 最终渲染；
5. 字幕 + 解说：字幕检查点 → Agent 编排 → 最终渲染；
6. 在字幕后、去重中、解说渲染中分别停止，然后验证原任务续做；
7. 两个访问密钥使用相同项目名，验证目录、任务、统计和清理互不影响。

真实模型、真实长视频和公网断线仍属于发布前验收测试，不能由纯单元测试完全替代。

## 当前基线（2026-07-27）

- 完整回归：186 项，全部通过；
- 前端 JavaScript 语法检查：9 个文件，全部通过；
- 本轮未重新计算覆盖率，历史百分比不作为当前发布判断；
- `worker.py` 中实际长时间子进程、真实公网断线和重量级模型分支仍需媒体验收，
  不能为了覆盖率数字而模拟第三方模型内部；
- Fish Speech、Chatterbox、Qwen TTS 的重量级入口不纳入默认单元测试，
  发布相应语音能力前必须分别执行真实 CUDA 冒烟测试。

上述数字用于发现薄弱边界，不作为“覆盖率达到即能发布”的替代标准。组合流程、
进程树停止、权限隔离、可解码输出和公网断线恢复仍需独立验收。
