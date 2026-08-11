# Web Gateway 模块架构

> 本文描述服务器端事实。浏览器只负责上传、设置和查看自己账号的数据，
> 不直接读取用户电脑磁盘，也不展示其他账号或整台服务器的容量。

## 五个可检查点业务阶段

- `subtitles`：软字幕/OCR/ASR、翻译、审核、字幕终稿；
- `publishing_planning`：通用 Agent 读取可选封面与下载器 MD/TXT，提交平台证据、源语言标题/Bio、中文展示字段、两级分类、hashtag 与封面布局；
- `recap_planning` + `recap_render`：Agent 编排、预览、TTS、最终成片；
- `dedup`：去重变换、字幕烧录和视频编码；
- `publishing_render`：服务端按已验收方案生成 PNG 封面、四行 `bio.txt` 与数据库就绪的 `publishing_metadata.json`。

`web_gateway/workflows.py` 描述模块顺序和持久检查点，不包含 OCR、翻译或
FFmpeg 业务代码。`web_gateway/worker.py` 负责组装模块并启动执行器。

`PUBLISHING_JOB` 与字幕和 RECAP 契约隔离。Agent 不写主机路径、不生成任意代码，
只提交受 schema 约束的语义方案；服务器在 `publishing_materials.py` 中确定性落盘。
可选图片/MD/TXT 缺失不会阻塞，平台标签则必须有材料证据。MD/TXT 明确给出的语言、平台和 AI 状态
会在 Agent 提交时由服务器再次绑定校验，防止 Agent 漂移。两级分类枚举固定为
`男频|女频|中性` 与 `魔幻|现代|古装`，当前落盘 JSON，后续数据库直接消费该契约。

## 服务器存储

```text
<storage_root>/
  用户/
    <用户名>__<access-key-id前8位>/
      <工程名>/
        字幕终稿/
        processed/
        解说/
        任务记录/
        .video-service/jobs/<job-id>/
```

访问密钥是权限边界，用户名只帮助服务器管理员识别目录。旧任务保留原路径，
不会被自动搬迁。

网页 `/api/v1/storage` 展示当前用户在服务器上的资源，不是浏览器电脑的磁盘。
服务器管理员可使用：

```powershell
python -m web_gateway.cli storage-report
python -m web_gateway.cli storage-cleanup --key-id <id> --category chunks
python -m web_gateway.cli storage-cleanup --key-id <id> --category chunks --execute
```

## 断点与取消

- 阶段完成后写入 `<job>/checkpoints/workflow.json`；
- 去重/编码还会写逐视频签名检查点；
- 服务异常退出将任务改为 `paused`；
- `/api/v1/jobs/<id>/resume` 复用同一任务、上传文件和检查点；
- 只有源指纹、配置签名、输出文件同时匹配才跳过编码；
- 停止解说渲染会终止当前 TTS/FFmpeg 进程树并禁止发布半成品。

## 前端模块

`web_gateway/static/js/` 分为：

- `core.js`：配置、API 和通用状态；
- `files.js`：文件夹选择和分片上传；
- `jobs.js`：任务、队列、日志和产物；
- `recap.js`：解说项目与时间轴；
- `preview.js`：随机帧和字幕预览；
- `resume.js`、`environment.js`、`storage.js`：恢复、预检和存储管理。

UI 只组织请求和展示结果，业务处理均在后端。
