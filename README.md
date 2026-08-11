# 本地短剧智能制作平台

面向短剧批量生产的一体化本地工具，覆盖四条可独立运行、也可自由组合的流水线：

1. 字幕获取与翻译：软字幕、画面 OCR、Whisper ASR、双源对齐、LLM 或 Codex Agent 翻译与审核。
2. 解说视频编排与剪辑：剧情分析、解说文案、片段时间轴、TTS、预览、校验和最终渲染。
3. Agent 发布物料与分类：读取下载器 MD/TXT 与封面，生成标题、Bio、hashtag、中文展示信息、剧集分类和分集封面方案。
4. 视频画面增强：裁剪、画中画、模糊背景、滤镜、变速、动态特效、背景音乐和硬件编码。

项目同时提供 Windows 桌面 GUI、命令行工具和可远程访问的 Web Gateway。所有视频处理均由本机 FFmpeg 与本地模型执行；请只处理自己拥有或获准修改的素材。

## 主要能力

### 字幕

- 自动选择软字幕、硬字幕 OCR 或语音 ASR，也可指定固定来源。
- OCR 与 ASR 可并行提取，按时间轴对齐后交给翻译与审核流程。
- 支持中文、英文、阿拉伯语等源语言与目标语言。
- 支持 API 模式与 Agent 文件桥接模式。
- 支持快速翻译和高级翻译，以及按题材加载 `glossaries/` 术语表。
- 支持双语字幕、覆盖原字幕、动态模糊遮罩、字体和字幕区域预览。
- 终稿保存在源工程目录的 `字幕终稿/`，原文与译文各保留一份标准 SRT。

### 解说剪辑

- 结构化 `recap_plan` 管理剧集、源区间、解说正文和成片时间轴。
- 分阶段 Agent 流程：剧情分析、初稿、独立审核、最终修订、最终验证。
- 服务端校验阶段令牌、制品哈希、读取记录和隔离审核上下文。
- 英语和阿拉伯语使用独立的模型路由与声纹库。
- 支持 Fish Speech S2、Chatterbox Multilingual 和兼容的英文 TTS 路由。
- 支持固定声纹试听、语速预算、响度策略、局部渲染、预览与最终成片。
- 成片发布到源工程目录的 `解说/`。

解说规则分别位于：

- [CODEX_RECAP_INIT.md](CODEX_RECAP_INIT.md)：Agent 初始化与通信协议。
- [RECAP_EDITOR_PLAYBOOK.md](RECAP_EDITOR_PLAYBOOK.md)：剧情理解和创作规范。
- [RECAP_ENGINE_IMPLEMENTATION.md](RECAP_ENGINE_IMPLEMENTATION.md)：运行时契约与实现边界。

### 画面增强与批处理

- 单文件、多个文件或目录第一层视频批处理。
- `custom`（自定义默认）、`light`、`medium`、`strong`、`deep` 五档强度预设，也可完全自定义。
- NVIDIA NVENC、AMD AMF、Intel QSV、Apple VideoToolbox 和 CPU 编码。
- 模糊背景画中画、比例标准化、固定帧率、镜像、亮度、缩放和变速。
- 动态扫光、星光、飞雪、流星、烟花等素材，可随机出现或全程循环。
- CRF 默认 23；数值越低质量越高、文件也越大。

### Agent 发布物料

- 这是字幕、解说之外的通用 Agent 能力，可与纯去重直接组合。
- 工程根目录第一层可放一个 PNG/JPG/WebP 原始封面和一个 MD/TXT 剧名简介；缺失时不阻塞任务。
- 下载器 MD/TXT 是剧名、简介、语言和归属平台的权威来源；阿语元数据生成阿语文案，英语元数据生成英语文案。
- Agent 只负责语义内容和封面安全位置，服务器用 Pillow 确定性生成金色分集数字封面。
- 平台有可靠证据时 hashtag 顺序为 `#平台 #fyp ...`；无法确认平台时以 `#fyp` 开头，绝不猜测平台。
- `bio.txt` 固定四行：本剧 AI 生成状态、标题、Bio、空格分隔的 5-7 个 hashtag。
- `publishing_metadata.json` 保存中文标题/简介、`男频|女频|中性`、`魔幻|现代|古装`、分类置信度与依据，作为后续数据库导入契约。
- “Agent 发布物料 + 二次去重”完成后，视频、对应封面和发布文案统一位于 `processed/`。
- 只勾“视频画面增强/二次去重”不会调用 Agent；发布物料是独立勾选能力，不需要单独页面。

## 流水线组合

GUI 和 Web 页面都可勾选一个或多个阶段。组合顺序固定为：

```text
字幕获取与翻译 -> Agent发布物料 -> 解说视频编排与剪辑 -> 画面增强
```

只启用字幕时不会重编码视频；只启用画面增强时不会调用 OCR、ASR、LLM 或 Agent。发布物料阶段不会运行 OCR/ASR，也不修改视频；它等待 Agent 方案通过后再由服务器渲染。已完成阶段会写入任务清单，可在中断后从最近的有效检查点继续。

## 目录约定

用户选取的视频源文件夹就是工程根目录。平台不会把代码目录当成成品目录。

```text
剧名/
├─ 原始视频.mp4
├─ cover.webp               # 可选，也支持 PNG/JPG
├─ 剧名.md                  # 可选，也支持 TXT
├─ 字幕终稿/
│  ├─ 原始视频.source.<语言>.srt
│  ├─ 原始视频.final.<语言>.srt
│  └─ manifest.json
├─ processed/
│  ├─ <去重成片>.mp4
│  ├─ <去重成片>_cover.png
│  ├─ bio.txt
│  └─ publishing_metadata.json
├─ 解说/
└─ 任务记录/
   └─ <任务名_时间_job短ID>/
      ├─ logs/
      ├─ agent/
      └─ manifest.json
```

Web 上传的临时数据位于服务器配置的 `.video-service/jobs/<job-id>/`，发布完成后按上面的工程结构归档。API 返回逻辑目录名，不泄露服务器绝对路径。

## 环境要求

- Windows 10/11；核心命令行也支持 macOS 和 Linux。
- Python 3.12 推荐。
- FFmpeg 与 FFprobe 可在 `PATH` 中找到，或由本地 GUI 显式指定。
- NVIDIA GPU 推荐用于 OCR、Whisper、TTS 和 NVENC 编码。

安装基础开发依赖：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

字幕与 ASR 环境可使用：

```powershell
.\setup_asr_windows.ps1
```

不同 PaddlePaddle CUDA 版本应按照本机驱动和 Paddle 官方安装源选择，不要与系统 Python 3.14 混装。

## 启动桌面端

```powershell
.\start_gui.bat
```

每次开始目录任务都会打开独立进度窗口；主窗口可以继续提交其他目录。GUI 会记忆字幕区域、字体、蒙版、硬件编码和输出质量等常用设置。

## 命令行

单视频画面处理：

```powershell
py -3.12 .\video_dedup.py input.mp4 output.mp4 --preset custom
```

完整目录流水线：

```powershell
py -3.12 .\batch_pipeline.py input_dir output_dir --config config.local.json --preset custom
```

解说项目：

```powershell
py -3.12 .\recap_cli.py --help
```

配置示例见 [config.example.json](config.example.json) 和 [recap/examples/project.example.json](recap/examples/project.example.json)。

## Web Gateway

安装并启动：

```powershell
py -3.12 -m pip install -r requirements-web.txt
.\start_web_gateway.ps1
```

默认仅应监听本机或受保护的反向代理。远程部署建议使用 HTTPS Tunnel，并为每个用户创建独立访问密钥。

服务端具有以下边界：

- 用户、任务、工程目录和发布制品隔离。
- 分片 SHA-256、单文件/单任务/账户容量限制、上传速率限制和磁盘余量检查。
- 远程配置只允许公开的渲染参数，不能指定 Python、FFmpeg、模型或任意可执行路径。
- API 模式拒绝回环、内网和保留地址的 LLM URL，并禁止跟随重定向。
- Agent 制品采用流式文件响应，避免大字幕或审核材料一次性载入服务进程内存。
- 任务取消会终止经过 PID、启动时间和可执行文件三重匹配的进程树，避免误杀复用 PID。

容量可通过环境变量调整：

```text
VIDEO_GATEWAY_MAX_FILE_SIZE
VIDEO_GATEWAY_MAX_JOB_UPLOAD_SIZE
VIDEO_GATEWAY_MAX_ACCOUNT_STORAGE
VIDEO_GATEWAY_MIN_FREE_SPACE
VIDEO_GATEWAY_MAX_UPLOAD_CHUNKS_PER_MINUTE
```

部署细节见 [WEB_GATEWAY_INTEGRATION.md](WEB_GATEWAY_INTEGRATION.md) 和 [WEB_GATEWAY_ARCHITECTURE.md](WEB_GATEWAY_ARCHITECTURE.md)。

## Agent 模式

1. 在 GUI 或 Web 页面生成 Agent 初始化命令。
2. 将命令粘贴到负责该账户的 Codex 对话。
3. Agent 完成带 nonce 的隔离子 Agent 能力探针并注册监听。
4. 程序提交任务；Agent 读取动态清单、字幕和阶段规则，持续发送工作心跳。
5. 服务端只接受满足完整性、索引、语言、时间轴、质量门槛和阶段证据的结果。

字幕、发布物料与解说任务共用注册对话，但拥有不同事件和响应契约。发布物料使用 `PUBLISHING_JOB`，不能返回字幕数组或解说时间轴；解说最终审核必须由隔离子 Agent 实际读取服务器保存的最新修订制品，复制哈希或自报“已审核”不能替代服务器证据。

## 两个产品版本

- 域名部署使用本分支的 Agent 复合版：支持字幕 Agent、发布物料 Agent、解说 Agent 与本地确定性渲染。
- `feature/api-only-platform` 保留为独立本地 API-only 版本，不覆盖本分支，也不作为域名服务入口。

## 声纹与模型

正式声纹清单位于 [recap/voices/library.json](recap/voices/library.json)，每个条目包含参考音频、参考文本、模型版本和试听文件。TTS 缓存键同时包含参考音频内容哈希，因此替换声纹文件后不会误用旧缓存。

模型权重和虚拟环境不提交到 Git：

```text
../.model-cache/
../.tts-envs/
```

Fish Speech 权重可能使用单独许可；商用前请自行确认所用模型、权重、声纹和素材的授权范围。

## 测试

```powershell
py -3.12 -m unittest discover -v
```

也可以按 [TESTING.md](TESTING.md) 运行 Web、字幕、解说、画面处理和前端语法检查。提交前至少执行：

```powershell
py -3.12 -m compileall -q .
py -3.12 -m unittest discover
git diff --check
```

## 安全与隐私

- 不要把 API Key、访问密钥、Cloudflare 凭据或 `.env` 提交到仓库。
- Web 访问密钥只用于认证调用者，不能替代 HTTPS。
- 公网服务应限制访问来源、定期清理任务、监控磁盘与失败重试。
- OCR、ASR、翻译和自动剪辑都可能出错，发布前应抽查字幕、时间轴、声音和版权状态。

## 许可证与责任

代码、第三方模型、模型权重、字体、声纹、音乐和动态素材可能采用不同许可证。使用者负责确认素材权利、模型许可、目标平台规则和当地法律要求。画面变换或重新编码不改变原素材版权，也不保证任何平台审核或推荐结果。
