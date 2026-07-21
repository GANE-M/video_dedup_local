# 短剧解说剪辑引擎实现说明

## 文档目的

这份文档用于让维护主工程的 Codex 对话准确理解已经验证的解说剪辑代码、数据流、
实现边界和迁移目标。它不是新剪辑任务的操作提示词；新剪辑对话的工作规则位于
`CODEX_RECAP_INIT.md`。

## 当前状态

解说剪辑能力已经正式迁入 `video-dedup-local` 主工程，当前开发分支为
`feature/recap-engine-main`。正式实现位于：

```text
video-dedup-local/recap/
video-dedup-local/recap_cli.py
video-dedup-local/recap_gui.py
```

桌面程序主窗口已经增加“解说剪辑”页面，可创建和载入项目、管理结构化片段、
选择并试听固定声纹、执行校验和响度测量、局部渲染、预览及最终渲染。程序内部
不运行自主 Agent，也不接入字幕翻译 Agent bridge。

原型仓库：

```text
E:\wangyang\Videos\短剧输出\小超人\recap-demo-worktree
```

主工程：

```text
E:\wangyang\Documents\Codexfile\climind\video-dedup-local
```

原型当前主分支提交为 `66792f7`。关键实现提交：

- `18a5c9c`：固定 Qwen 声纹、逐段实测响度归一化和最终混音音量修复；
- `d51a004`：完整解说、原声、混音和无声画面母版；
- `e446625`：源区间检查和渲染后全局画面指纹去重；
- `9f880de`：多项目配置、独立男女声缓存和美国狙击手完整案例。

这些提交只作为迁移依据。正式模块没有 cherry-pick 原型提交，也没有把原型中的
剧名、素材路径或人工时间轴写死到 Python 源码。

## 正式模块与路径

```text
recap/
  models.py            项目、片段、声纹及稳定缓存键
  project_store.py     UTF-8 原子保存、版本、比较和回退
  timeline.py          源文件、音轨、越界和同集区间重叠校验
  visual_dedup.py      全片段对感知画面重复检查和 JSON 报告
  voice_library.py     声纹清单、资产解析和项目隔离缓存
  qwen_tts.py          Qwen3-TTS 固定参考声纹批量生成适配器
  loudness.py          源节目能量加权响度及两遍 loudnorm
  renderer.py          片段、四类母版、最终封装和解码检查
  cli.py               机器可读命令实现
  voices/library.json  正式声纹库
  examples/project.example.json
recap_cli.py           CLI 入口
recap_gui.py           主 GUI 的解说剪辑工作区
```

项目 JSON 可保存在用户选择的任意位置；推荐每个项目单独目录。每次结构化编辑在
项目文件旁的 `.recap_versions/<project_id>/vNNNN.json` 保存不可覆盖的快照。渲染
产物写入 `<output_root>/vNNNN/`，缓存写入 `<output_root>/.recap_cache/`。声纹库路径
为 `recap/voices/library.json`，男女声参考和 10–15 秒试听文件位于各自子目录。

## 原型文件与职责

### `render_recap_pilot.py`

这是当前完整渲染入口，包含：

- 两个已验证项目的配置和人工编排时间轴；
- 源视频路径解析；
- 时间轴源区间校验；
- 解说与原声片段渲染；
- 逐句解说字幕排版；
- 渲染后画面感知指纹去重；
- 完整无声画面母版合成；
- 解说母带、原声母带和完整混音母带合成；
- 最终 MP4 封装。

主要函数及真实作用：

| 函数 | 作用 |
|---|---|
| `episode_path()` | 根据当前项目配置解析某一集源视频 |
| `validate_timeline_intervals()` | 在渲染前拒绝同一集内重叠的源时间区间和越界区间 |
| `segment_frame_hashes()` | 以每秒 2 帧抽样，裁掉底部字幕区域后计算灰度 dHash |
| `duplicate_run()` | 搜索两段素材之间连续近似相同的画面序列 |
| `validate_rendered_visual_uniqueness()` | 比较时间轴上所有片段对并输出 `duplicate_report.json` |
| `generate_qwen_voices()` | 为所有解说段调用固定声纹生成器并复用项目独立缓存 |
| `normalize_narration()` | 对单段解说执行实测两遍响度归一化和必要的校正 |
| `narration_filter()` | 按句拆分解说文字并生成字幕显示区间 |
| `build_segment()` | 根据 `narration` 或 `original` 模式生成单个音视频片段 |
| `join_video_segments()` | 只拼接画面，生成完整无声视频母版 |
| `render_audio_stem()` | 把一种音频按时间偏移铺到完整时间轴 |
| `mix_audio_masters()` | 合并解说与原声母带，明确使用 `amix normalize=0` |
| `mux_master()` | 把无声画面母版与完整音频母带封装成最终 MP4 |

### `generate_qwen_voice.py`

这是 Qwen3-TTS 固定声纹生成器：

1. 如果指定声纹参考不存在，使用 VoiceDesign 模型只设计一次参考音频；
2. 保存参考音频和准确参考文本；
3. 使用 Qwen3-TTS Base 模型建立 voice clone prompt；
4. 以后所有解说段复用同一个 prompt 和项目独立缓存；
5. 已有缓存完整时不重新生成，避免随机音色变化。

当前已验证的两个 profile：

- `calm_female` → `voice_reference/calm_female_narrator.wav`
- `calm_male` → `voice_reference/calm_male_narrator.wav`

原型使用的模型：

```text
Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign
Qwen/Qwen3-TTS-12Hz-1.7B-Base
```

## 当前运行数据流

```text
人工选定的项目配置和时间轴
        ↓
校验源文件、时长和同集区间重叠
        ↓
使用固定 voice profile 生成或复用解说 WAV
        ↓
把每段解说归一化到项目固定 LUFS
        ↓
分别渲染 narration/original 片段
        ↓
对所有片段对应的源画面做全局 dHash 去重
        ↓
生成 video_master_silent.mp4
        ↓
生成 narration_master.wav 和 original_master.wav
        ↓
生成 complete_audio_master.wav
        ↓
封装最终 MP4
```

## 时间轴数据模型

正式实现使用可序列化的 UTF-8 JSON 项目文件，不再使用硬编码 Python tuple。
完整示例见 `recap/examples/project.example.json`，核心结构如下：

```json
{
  "schema_version": 1,
  "project_id": "american-sniper-demo",
  "source_root": "...",
  "voice_id": "calm_male_01",
  "target_duration_seconds": 470,
  "segments": [
    {
      "segment_id": "seg-001",
      "episode": 9,
      "source_start": 54.0,
      "source_end": 71.0,
      "mode": "narration",
      "narration_text": "...",
      "purpose": "hook",
      "revision": 1,
      "rendering": {},
      "cache_key": "..."
    }
  ]
}
```

稳定 `segment_id` 已贯穿结构化编辑、版本比较、局部渲染和缓存失效。

## 音画行为

### 解说片段

- 源视频声音关闭；
- 如果前一段是原声，解说默认在新画面开始约 0.5 秒后出现；
- 解说字幕按英文句号、问号和感叹号拆分，按词数比例分配显示时长；
- 字幕默认位于画面高度约 12% 的上部区域；
- 当前实现用固定参考声纹逐段生成 WAV，而不是把整部解说一次生成后物理切分；
- 音色一致来自同一参考音频、参考文本、clone prompt 和独立缓存。

### 原声片段

- 画面严格在 `source_end` 处切断；
- 当下一段是解说时，原声音频可比画面多保留约 0.5 秒；
- 这段尾音只存在于音轨，旧画面不延长；
- 从解说进入原声不添加静音空白，原声音量使用约 0.3 秒渐入；
- 原声尾音使用约 0.5 秒淡出。

### 完整母版

- 每个片段的视频先拼成无声画面母版；
- narration 和 original 音频分别按输出时间偏移铺成完整 WAV；
- 两条母带最后混合；
- FFmpeg `amix` 必须设置 `normalize=0`，否则输入数量会把最早出现的解说音量除低。

## 响度策略

每个项目先测量全部源视频的 integrated loudness，并按各集时长做能量加权。该结果
成为整部解说的固定目标，而不是让每段配音各自决定音量。

已验证案例：

- Interstellar General：源节目约 `-10.34 LUFS`，解说目标 `-10.3 LUFS`；
- American Sniper：源节目约 `-17.97 LUFS`，解说目标 `-18.0 LUFS`。

单段解说先执行 FFmpeg loudnorm 测量，再执行带 measured 参数的第二遍归一化；若
限峰造成结果偏离超过约 0.15 dB，则继续进行小幅增益校正。最终仍使用 limiter
控制峰值。

## 画面去重实现

去重分为两层。

### 渲染前区间检查

- 校验起止时间合法；
- 校验源文件存在；
- 校验结束时间未超过源视频；
- 按集排序，拒绝任何配置时间区间重叠。

### 渲染后感知检查

- 每秒抽取 2 帧；
- 裁取画面顶部约 78%，减少底部字幕差异干扰；
- 缩放为 9×8 灰度图并计算 64 位 dHash；
- 汉明距离不超过 3 视为近似同帧；
- 连续 3 个采样帧相同，即连续约 1.5 秒，判定为重复；
- 比较所有片段组合，包括非相邻片段和不同集；
- 因此能够发现下一集开头复用上一集结尾的回顾镜头；
- 发现冲突后写入 `duplicate_report.json` 并阻止最终合成。

当前检查比较不同片段，不检查单个片段内部的自重复。正式模块可以保留这一行为，
并另行增加可配置的片段内循环检测。

## 已验证成片

### Interstellar General

- 输出约 464.52 秒；
- 固定女性声纹；
- 重复报告为空；
- 解说、原声、混音和无声画面母版完整生成。

### American Sniper: The Kid of Guns

- 10 集源视频；
- 输出约 471.38 秒；
- 固定男性声纹；
- 解说母带实测约 `-17.91 LUFS`；
- 第一轮检查发现两处跨集回顾镜头重复并阻止合成；
- 替换画面后报告为空并完成最终成片；
- 结尾停在子弹飞向靶纸、结果即将揭晓的位置。

这些案例证明渲染、固定声纹、响度、完整母版和跨集去重链路可运行，不等于剧情
理解和镜头编排已经自动化。

## 尚未实现的能力

以下属于外部 Codex 的剧情理解职责，或刻意暂缓的增强项，不能向用户声称程序会
自主完成：

- 自动读取任意 SRT 后直接生成可靠的完整故事梗概；
- 自动生成、评分并选择多个钩子；
- 基于关键帧和视频模型自动选择全部高光；
- 自动从字幕或视频理解剧情并决定整部成片时间轴；
- 自动生成、评分并选择多个钩子；
- 基于视频模型自动选择全部高光；
- 在程序内部理解自然语言修改要求；
- 通用多语言声纹质量自动评分；
- 单个片段内部的循环画面检测（当前比较不同片段的所有组合）。

结构化项目、声纹库/试听、局部渲染、缓存失效、版本比较与回退，以及 GUI 工作区
已经实现。剧情理解和自然语言编辑继续由外部 Codex 对话完成。

## 正式主工程模块边界

```text
recap/
  models.py            项目、片段、声纹和渲染产物模型
  project_store.py     项目 JSON、版本和缓存索引
  timeline.py          时间轴校验、输出偏移和局部依赖计算
  visual_dedup.py      源区间与感知画面去重
  voice_library.py     声纹清单、参考文件和试听样本
  qwen_tts.py          固定声纹 TTS 适配器
  loudness.py          源节目测量和解说归一化
  renderer.py          片段、母版和最终封装
  cli.py               供 Codex 对话调用的稳定命令
```

不要把故事理解或自然语言推理硬编码进渲染模块。Codex 对话负责产生或修改项目
JSON；程序负责验证、渲染、缓存、报告和版本管理。

## 正式 CLI

从主工程根目录运行，所有命令输出 UTF-8 JSON：

```text
py -3.12 recap_cli.py inspect-sources --source <folder> [--pattern *.mp4]
py -3.12 recap_cli.py create-project --project <project.json> --project-id <id> --source <folder> --output <folder>
py -3.12 recap_cli.py validate-project --project <project.json>
py -3.12 recap_cli.py measure-loudness --project <project.json>
py -3.12 recap_cli.py list-voices
py -3.12 recap_cli.py preview-voice --voice-id calm_female
py -3.12 recap_cli.py render-segment --project <project.json> --segment-id <id>
py -3.12 recap_cli.py render-preview --project <project.json>
py -3.12 recap_cli.py render-final --project <project.json>
py -3.12 recap_cli.py update-segment --project <project.json> --segment-id <id> --changes-json <json>
py -3.12 recap_cli.py delete-segment --project <project.json> --segment-id <id>
py -3.12 recap_cli.py project-diff --project <project.json> --from <version> --to <version>
py -3.12 recap_cli.py rollback-project --project <project.json> --version <version>
```

命令输出应提供机器可读 JSON，便于新 Codex 对话定位失败片段、重复区间、缓存命中
和产物路径，而不是解析控制台自然语言。

## 声纹库正式数据模型

最低字段：

```json
{
  "voice_id": "calm_male_01",
  "display_name": "Calm Male 01",
  "gender": "male",
  "languages": ["English"],
  "style": ["calm", "documentary", "grounded"],
  "reference_audio": "voices/calm_male_01/reference.wav",
  "reference_text": "I will tell this story...",
  "default_speed": 1.0,
  "target_loudness_mode": "match_source_program",
  "preview_text": "...",
  "preview_audio": "voices/calm_male_01/preview.wav",
  "engine": "qwen3_tts_12hz_1_7b_base"
}
```

参考音频和参考文本必须成对保存。项目缓存键至少包含 `voice_id`、正文、语言、
速度、模型版本和生成参数，防止不同声纹或参数误用同名 WAV。

## 主工程迁移约束

- 主工程目前可能存在未提交修改；不得 reset、checkout、删除或擅自 stash。
- 使用独立功能分支或 worktree，只提交本次新增和明确修改的文件。
- 不修改原始素材，不提交大型缓存、模型或成片。
- 不把原型的素材路径、输出路径、虚拟环境路径或具体剧名写死到正式模块。
- 保持现有字幕提取、翻译、视频处理和下游返回结构不变，优先增量添加。
- 不把解说功能接入字幕翻译 Agent bridge；两者生命周期和任务契约不同。
- `CODEX_RECAP_INIT.md` 是外部 Codex 对话初始化入口，不是运行时代码配置。

## 迁移验收标准

完成主工程整合至少要证明：

1. 可以用项目 JSON 渲染两个不同剧集，而不修改 Python 源码中的时间轴。
2. 两个项目的输出、声纹和缓存相互隔离。
3. 固定男女声预设能够试听，并在整部视频保持同一身份。
4. 解说和原声母版长度与视频母版一致。
5. `amix normalize=0`，开头解说响度与后续一致。
6. 同集重叠、非相邻重复和跨集回顾镜头都能被测试捕获。
7. 重复存在时不会生成最终成片。
8. 时间轴修改后能够只重建受影响片段和必要母版。
9. 每次修改保留版本并可回退。
10. 最终 MP4 通过完整 FFmpeg 解码检查。

迁移完成后更新本文件的“当前状态”和正式模块路径，避免后续 Codex 对话继续把
独立原型误认为主工程正式实现。

## 正式迁移验证记录（2026-07-21）

- `py -3.12 -m unittest discover -v`：主工程 122 项测试全部通过；
- `test_recap.py`：14 项专项测试通过，包含真实 FFmpeg 解说/原声端到端渲染、
  四类母版等长、目标响度和最终完整解码；
- 两套固定声纹试听已实际生成，女声 13.52 秒，男声 12.64 秒；
- 使用 Interstellar General 第 1 集实际素材与 `calm_female` 完成 7.20 秒小样，
  narration master 实测约 -10.42 LUFS；
- 使用 American Sniper 第 1 集实际素材与 `calm_male` 完成 7.76 秒小样，
  narration master 实测约 -18.04 LUFS；
- 两个实际素材小样均通过重复检查和最终解码，运行输出位于系统临时目录，未提交
  大型成片或缓存。
