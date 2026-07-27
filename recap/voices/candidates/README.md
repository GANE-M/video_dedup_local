# Fish S2 音色候选池

这里保存尚未进入正式声纹库的 Fish Speech S2 合成候选音色。

生成命令：

```powershell
py -3.12 tools\build_fish_voice_pool.py --language English --count 8 --start-seed 202600
py -3.12 tools\build_fish_voice_pool.py --language Arabic --count 8 --start-seed 202700
```

每批候选共用一次模型加载。生成后的 `manifest.json` 会记录种子、参考文本、
音频路径、时长、峰值、静音比例、削波比例和技术分。候选默认是 `pending`，
不会出现在 GUI 或网页的正式下拉框中。

生成目录中的 `index.html` 可以直接打开逐个试听。需要用 Whisper 检查错读时：

```powershell
.\.venv-asr\Scripts\python.exe tools\validate_voice_library.py `
  --manifest recap\voices\candidates\fish_s2\english\manifest.json `
  --manifest recap\voices\candidates\fish_s2\arabic\manifest.json
```

试听后再填写性别、年龄组、角色类型和风格。只有确认无明显噪声、错读、断句或
不自然音色，并取得 8.5 分以上的候选，才复制到正式目录并写入
`recap/voices/library.json`。不要将未经授权的真人声纹加入正式库。
