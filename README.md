# 本地视频变换工具

这是从“易剪媒”的参数设计中重新实现的独立本地版本。它不调用易剪媒服务器，不需要登录或会员，FFmpeg 命令全部在本机生成。

> 仅用于自己拥有或获准修改的视频。修改编码或画面不改变素材的版权归属，也不保证任何平台的审核或推荐结果。

## 图形界面

双击 `start_gui.bat` 即可启动。界面支持任意多选视频或直接选择整个目录进行批量处理；也可以选择输出和背景音乐，并调整画面、时间、声音与输出质量参数。配置可以保存为 JSON 后重复使用。

界面顶部的“并行任务”用于控制同时运行的完整任务组数量，默认 2。现在的任务粒度是“一个目录/一组多选文件 = 一个任务”：同一个目录里的视频会在该任务窗口内按顺序处理，不会把目录里的每个视频都拆成独立并发任务。每次点击“开始处理”都会弹出一个新的任务进度窗口，主窗口可以继续选择下一个目录并启动新任务。

完整流水线顺序是“自动字幕处理 → 视频去重处理 → 写入成片”。字幕会先从原始视频中提取/识别并翻译，再根据裁剪、变速参数校正时间轴，最后写入去重后的视频，避免去重变换影响字幕识别或导致字幕偏移。

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
- `medium`：增加镜像、轻微变速和淡入淡出。
- `strong`：更明显的裁边、色彩叠加和速度变化。

## GPU 加速

界面的“输出质量”页可选择 `auto / nvidia / amd / intel / apple / cpu`。默认 `nvidia` 会直接使用 NVIDIA NVENC；Mac 选择 `apple` 使用 VideoToolbox；选择 `auto` 时会自动检测可用硬件编码器。画面滤镜仍可能在 CPU 执行，最终视频编码优先由 GPU/硬件编码器完成。

## 字幕处理

界面新增“字幕”页，支持自动字幕流水线：

- 自动检测视频是否有软字幕轨道。
- 默认按硬字幕短剧处理：用 PaddleOCR 识别画面硬字幕；也可选择“自动：软字幕→硬字幕OCR”优先利用软字幕轨道。只有手动选择“语音识别”或“三段自动兜底”时才会使用 Faster-Whisper。
- 自动调用 OpenAI-compatible LLM 接口翻译字幕。
- 自动将翻译字幕封装为软字幕，或烧录到画面；GUI 不需要手动选择/导出字幕文件。
- 两种烧录形式：
  - `双语字幕`：不遮住原字幕，把新字幕自动放在原字幕上方，形成双语字幕。
  - `覆盖原字幕`：优先用 OCR 自动识别原字幕区域，再用白色半透明蒙版遮住旧字幕并叠加新字幕；OCR 不可用或识别失败时回退到手动百分比参数。

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

LLM 翻译支持字幕批次并发，默认 3 路：

```powershell
python .\subtitle_tool.py translate "D:\video\input.srt" "D:\video\input_en.srt" --provider openai-compatible --target-language English --parallel-batches 3
```

硬字幕不能被真正删除；工具采用“遮盖旧字幕区域 + 烧录新字幕”的方式实现视觉替换。无字幕视频可通过 Faster-Whisper 先生成字幕。

硬字幕 OCR 需要 PaddleOCR。你当前如果只处理画面硬字幕，不需要安装 faster-whisper：

```powershell
pip install paddleocr paddlepaddle pillow
```

只有需要“无字幕视频语音识别”时，才安装：

```powershell
pip install faster-whisper
```

无字幕语音识别：

```powershell
python .\subtitle_tool.py transcribe "D:\video\input.mp4" "D:\video\input_asr.srt" --device cuda --model-size medium
```

自动估计硬字幕遮盖区域：

```powershell
python .\subtitle_tool.py detect-region "D:\video\input.mp4"
```

日志会输出建议的“遮盖起点高度”和“遮盖高度”，可填回界面滑块。

## Docker 运行字幕工具

如果不想在本机 Python 3.14 环境里安装 Whisper，可以用 Docker。Docker 内部使用独立 Python 3.11，不会影响系统 Python，也不会影响 GUI 里的其他功能。

GUI 也支持 Docker OCR 后端：字幕页里选择 `Docker OCR` 后，点击“开始处理”会临时启动一个容器；任务完成后容器自动退出，不需要常驻后端。只需要 Docker Desktop 本身处于运行状态。

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
