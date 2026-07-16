# 本地视频变换工具

这是从“易剪媒”的参数设计中重新实现的独立本地版本。它不调用易剪媒服务器，不需要登录或会员，FFmpeg 命令全部在本机生成。

> 仅用于自己拥有或获准修改的视频。修改编码或画面不改变素材的版权归属，也不保证任何平台的审核或推荐结果。

## 图形界面

双击 `start_gui.bat` 即可启动。界面支持任意多选视频或直接选择整个目录进行批量处理；也可以选择输出和背景音乐，并调整画面、时间、声音与输出质量参数。配置可以保存为 JSON 后重复使用。

界面顶部的“并行任务”控制每个目录内部同时处理的视频数量和 GUI 同时运行的任务组数量，默认且最多为 5。“一个目录/一组多选文件”仍是一个独立任务窗口，但目录内的视频会并发处理。ASR 和 LLM 请求分别使用跨进程、跨文件夹的全局 5 槽位，避免多个任务叠加成 25 路请求。每次点击“开始处理”都会弹出新的任务进度窗口，主窗口可以继续启动下一个目录。500 条以内的单个视频字幕通常整段发送给 AI；超过 500 条时按每 500 条分批发送。初译采用稳定索引对象：模型返回被截断时会先安全抢救已经完整闭合的字幕条目，再携带整集 OCR/ASR 上下文只补发缺失索引；默认最多请求三轮，第二轮起使用全局 2 槽位以降低服务端压力，不产生断点缓存文件。

目录任务的完整流水线顺序是“并发字幕提取与初译 → 每集完整语义审核 → 全剧实体一致性审核 → 并发视频去重与字幕写入”。字幕会先从原始视频中提取/识别并翻译，待同一目录全部视频准备完毕后统一人物、家族、地点和称谓，再根据裁剪、变速参数校正时间轴并写入成片，避免去重变换影响字幕识别或导致字幕偏移。双源模式仅在 OCR 与 ASR 同时失败时重跑字幕来源阶段，最多三次；只要任一来源成功就直接降级继续，不重复已经可用的来源。

## 快速使用

电脑已安装易剪媒时，程序会自动找到它附带的 FFmpeg。也可以安装自己的 FFmpeg，或者通过参数指定路径。

处理一个视频：

```powershell
python .\video_dedup.py "D:\video\input.mp4" "D:\video\output.mp4"
```

也可以使用 PowerShell 包装入口：

```powershell
.\run.ps1 "D:\video\input.mp4" "D:\video\output.mp4" -Preset medium
```

批量处理目录：

```powershell
python .\video_dedup.py "D:\video\inputs" "D:\video\outputs" --preset medium --seed 2026
```

查看命令但不执行：

```powershell
python .\video_dedup.py input.mp4 output.mp4 --dry-run
```

## 预设

- `light`：轻微裁边和色彩调整。
- `medium`：默认不镜像，使用轻微变速和淡入淡出。
- `strong`：更明显的裁边、色彩叠加和速度变化。

## GPU 加速

界面的“输出质量”页可选择 `auto / nvidia / amd / intel / apple / cpu`。默认 `nvidia` 会直接使用 NVIDIA NVENC；Mac 选择 `apple` 使用 VideoToolbox；选择 `auto` 时会自动检测可用硬件编码器。画面滤镜仍可能在 CPU 执行，最终视频编码优先由 GPU/硬件编码器完成。

## 字幕处理

每次启用字幕流水线时，程序会在 `logs/translation-records/<运行时间-进程号>/` 下为每个视频保存一份 UTF-8 JSON 诊断记录，并额外保存 `series-consistency.json`。记录包含 OCR/软字幕原文、ASR 原文、清洗文本、置信分、初译、整集审核报告、终稿、全剧实体统一决策及失败信息，不包含 API Key。记录采用阶段性原子写入，因此翻译或后续编码中途失败时也能保留已经完成的分析数据。可用 `--translation-log-dir` 改变记录根目录。

题材术语表默认放在项目根目录的 `glossaries/`。GUI 会扫描其中除 `template_*.json` 外的 JSON 文件，并在目标语言旁提供手动选择和刷新按钮；默认不使用术语表。选中的术语表会同时注入初译和整集审核 Prompt，并写入翻译诊断记录。当前内置 `chinese_history_zh_en_ar.json`（中国历史/古装，中英阿三语），可复制模板继续增加现代商战、医疗、法律等题材。命令行可使用 `--glossary-file <json路径>`；Docker GUI 会把所选文件临时映射进容器，无需重新构建镜像。

界面新增“字幕”页，支持自动字幕流水线：

- 自动检测视频是否有软字幕轨道。
- 默认按硬字幕短剧处理：用 PaddleOCR 识别画面硬字幕；也可选择“自动：软字幕→硬字幕OCR”优先利用软字幕轨道。只有手动选择“语音识别”或“三段自动兜底”时才会使用 Faster-Whisper。
- 自动调用 OpenAI-compatible LLM 接口翻译字幕。
- 自动将翻译字幕封装为软字幕，或烧录到画面；GUI 不需要手动选择/导出字幕文件。
- 原字幕语言可选择“自动/中文/英语/阿拉伯语”；翻译源语言也可单独选择，默认自动交给 LLM 判断。
- 阿拉伯语硬字幕会使用 EasyOCR；中文/英文默认使用 PaddleOCR。
- 两种烧录形式：
  - `双语字幕`：不遮住原字幕，新字幕自动放在顶部，避免贴着旧字幕导致换行重叠。
  - `覆盖原字幕`：优先用 OCR 自动识别原字幕区域，再用白色半透明蒙版遮住旧字幕并叠加新字幕；OCR 不可用或识别失败时回退到手动百分比参数。
- 字幕页支持滚动，并提供可折叠的“字幕区域预览”：可用当前第一个视频或手动选择视频随机抽帧，用滑块或拖动画面方框校准白色蒙版位置。

GUI 里需要填写 API Key、接口地址和模型名。DeepSeek/OpenAI-compatible 接口均可；如果使用 DeepSeek V4 Flash，请把模型名填成 DeepSeek 后台显示的准确模型 ID。

完整单视频流水线命令也可单独使用：

```powershell
python .\batch_pipeline.py "D:\video\input.mp4" "D:\video\output.mp4" --preset medium --config .\config.example.json --enable-subtitles --subtitle-source hard-ocr --target-language English --llm-model deepseek-v4-flash --subtitle-layout replace --subtitle-cover
```

硬字幕视频也可用 PaddleOCR 估计遮盖区域。GUI 中“自动识别原字幕区域”默认开启；如果未安装 PaddleOCR，会自动使用手动遮盖起点/高度。

命令行也可单独使用：

```powershell
python .\subtitle_tool.py detect "D:\video\input.mp4"
python .\subtitle_tool.py extract "D:\video\input.mp4" "D:\video\input.srt"
python .\subtitle_tool.py hard-ocr "D:\video\input.mp4" "D:\video\input_ocr.srt"
python .\subtitle_tool.py translate "D:\video\input.srt" "D:\video\input_en.srt" --provider none
python .\subtitle_tool.py render "D:\video\input.mp4" "D:\video\input_en.srt" "D:\video\output_bilingual.mp4" --mode burn --layout bilingual
python .\subtitle_tool.py render "D:\video\input.mp4" "D:\video\input_en.srt" "D:\video\output_replace.mp4" --mode burn --layout replace --cover --cover-auto-detect --cover-color white
```

`--provider none` 不联网，只复制字幕文件，适合先手动编辑。若要自动翻译，可设置 `OPENAI_API_KEY`，并使用：

```powershell
python .\subtitle_tool.py translate "D:\video\input.srt" "D:\video\input_en.srt" --provider openai-compatible --target-language English
```

LLM 翻译默认按视频整段发送；当单个视频超过 500 条字幕时，自动按每 500 条分批发送。`--parallel-batches` 仅为兼容旧命令保留：

```powershell
python .\subtitle_tool.py translate "D:\video\input.srt" "D:\video\input_en.srt" --provider openai-compatible --target-language English
```

硬字幕不能被真正删除；工具采用“遮盖旧字幕区域 + 烧录新字幕”的方式实现视觉替换。无字幕视频可通过 Faster-Whisper 先生成字幕。

硬字幕 OCR 需要 PaddleOCR。你当前如果只处理画面硬字幕，不需要安装 faster-whisper：

```powershell
pip install paddleocr paddlepaddle pillow
pip install easyocr
```

NVIDIA 显卡可把中文/英文 PaddleOCR 改为 CUDA 版。Windows + Python 3.12 示例（CUDA 12.9）：

```powershell
.\.venv-ocr\Scripts\python.exe -m pip uninstall -y paddlepaddle
.\.venv-ocr\Scripts\python.exe -m pip install paddlepaddle-gpu==3.3.0 -i https://www.paddlepaddle.org.cn/packages/stable/cu129/
```

界面中的“OCR设备”选“自动”会优先使用 `gpu:0`，不可用时回退 CPU；选 `cuda` 则要求 CUDA 必须可用。应用会自动注册 pip 安装在虚拟环境中的 cuBLAS/cuDNN DLL，无需修改系统 PATH。

只有需要“无字幕视频语音识别”时，才安装：

```powershell
pip install faster-whisper
```

无字幕语音识别：

```powershell
python .\subtitle_tool.py transcribe "D:\video\input.mp4" "D:\video\input_asr.srt" --device cuda --model-size medium
```

Faster-Whisper 使用 CTranslate2；`--device cuda` 会使用 NVIDIA CUDA + float16，`--device auto` 会优先 CUDA、不可用时回退 CPU int8。

自动双源流水线会启用 Whisper 单词级时间戳，并在本地把单词分配到对应 OCR/软字幕时间段；不会再用较长 ASR 段扩大 OCR 字幕时间。同语言双源使用文本一致度、ASR 单词置信度、视觉文本洁净度和时间匹配质量评分；跨语言双源不比较字符相似度，改用视觉文本质量、ASR 置信度、时间匹配和 OCR 持续稳定性评分。启用智能审核后，主模型先翻译全部字幕，审核模型再读取整集有序上下文；置信阈值（默认 `0.82`）只标记风险，不再让高置信字幕绕过审核。单源和双源审核都只返回需要修改的稳定索引操作（替换、删除、合并），本地会限制总修改比例、删除数量和持续时间；合并只允许连续 2-8 条且最多覆盖 6 秒。全部视频翻译完成后，程序使用各集审核模型发现的实体证据做一次全剧一致性审核。每项改名必须引用至少两集真实存在的字幕索引，本地确认对应行确实出现旧名或规范名，并拒绝冲突、反向和链式替换；应用后会同步更新单集 JSON 终稿记录。任何额外审核失败都会保留上一阶段终稿继续处理。

Ruta 暂未提供 V4 Pro 时，主模型和审核模型都可填写 `deepseek-v4-flash`；以后只需把“审核模型”改为 `deepseek-v4-pro`。

自动估计硬字幕遮盖区域：

```powershell
python .\subtitle_tool.py detect-region "D:\video\input.mp4"
```

日志会输出建议的“遮盖起点高度”和“遮盖高度”，可填回界面滑块。

## Docker 运行字幕工具

如果不想在本机 Python 3.14 环境里安装 Whisper，可以用 Docker。Docker 内部使用独立 Python 3.11，不会影响系统 Python，也不会影响 GUI 里的其他功能。

GUI 默认使用本机 Python 3.12 OCR 环境，也支持 Docker OCR 后端：字幕页里选择 `Docker OCR` 后，点击“开始处理”会临时启动一个容器；任务完成后容器自动退出，不需要常驻后端。只需要 Docker Desktop 本身处于运行状态。

首次使用硬字幕 OCR 前先构建镜像：

```powershell
docker build --build-arg INSTALL_OCR=1 -t video-dedup-local:ocr .
```

构建默认镜像（FFmpeg + Faster-Whisper）：

```powershell
cd E:\wangyang\Documents\Codexfile\climind\video-dedup-local
docker build -t video-dedup-local:latest .
```

把视频放到 `video-dedup-local\work` 目录后运行：

```powershell
.\docker-run.ps1 transcribe /work/input.mp4 /work/input_asr.srt --device cpu --model-size medium
```

如果你的 Docker Desktop 已配置 NVIDIA GPU，也可以尝试：

```powershell
docker run --rm -it --gpus all -v "${PWD}\work:/work" video-dedup-local:latest transcribe /work/input.mp4 /work/input_asr.srt --device cuda --model-size medium
```

OCR 镜像比较大，需要硬字幕自动定位时再构建：

```powershell
docker build --build-arg INSTALL_OCR=1 -t video-dedup-local:ocr .
docker run --rm -it -v "${PWD}\work:/work" video-dedup-local:ocr detect-region /work/input.mp4
```

也可以用 Compose：

```powershell
docker compose build subtitle-tool
docker compose run --rm subtitle-tool transcribe /work/input.mp4 /work/input_asr.srt --device cpu --model-size medium
```

注意：Tkinter 图形界面不建议跑在 Docker 里；Windows 本地 GUI 继续用 `start_gui.bat`，Docker 主要负责字幕识别、OCR、翻译、烧录等命令行任务。

## 翻译方案怎么选

不使用 LLM 也可以翻译，常见选择：

- DeepL：质量很好，免费额度有限，超出收费；适合字幕直译。
- Google Translate / Google Cloud Translation：质量稳定，通常云 API 收费；非官方免费接口不稳定。
- Microsoft Translator：云 API，通常有免费额度，超出收费。
- LibreTranslate：可自建，开源；公共实例常有限流，质量一般。
- Argos Translate：本地离线、开源免费；质量比 DeepL/LLM 弱，但隐私最好。
- NLLB / MarianMT：本地模型，免费开源；部署和语言对选择更麻烦，字幕口语化效果不如 LLM。

如果是短剧字幕，我的建议：

- 要快、便宜、可控：先用 DeepL/Google/Microsoft 这类传统翻译 API。
- 要口语自然、剧情语气更顺：用 LLM。
- 要完全离线：Argos Translate 或 NLLB，但需要接受翻译质量下降。

## 自定义

复制 `config.example.json`，修改参数后运行：

```powershell
python .\video_dedup.py input.mp4 output.mp4 --preset medium --config .\my-config.json
```

背景音乐使用本地文件绝对路径：

```json
{
  "background_music": "D:\\music\\background.mp3",
  "music_volume": 0.08
}
```

批量任务也可以从指定目录（包括子目录）为每个视频随机选择一首音乐：

```json
{
  "background_music": null,
  "background_music_dir": "D:\\music\\library",
  "music_volume": 0.08
}
```

配置文件只需要填写想覆盖的字段，不必复制全部字段。
