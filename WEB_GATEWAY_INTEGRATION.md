# 网页接入模块交付说明

`web_gateway` 是一套可独立运行、也可被第三方网页 APP 集成的参考实现。它不会
读取或展示 `E:\wangyang\Videos\短剧输出` 下的其他剧集；每个访问密钥只能查看
自己创建的任务。默认 Agent 会话令牌只允许访问该密钥名下的任务；单任务令牌仅作
兼容和故障隔离使用。

当前 Windows 服务通过 Cloudflare Named Tunnel 发布到
`https://upload.andymori.uk`，本机网关监听 `127.0.0.1:8788`。视频仍保存在本机，
不依赖对象存储；HTTP 接口与页面可以继续被其他 APP 集成。

## 1. 安装与启动

使用已有 Python 3.12 OCR 环境：

```powershell
cd E:\wangyang\Documents\Codexfile\climind\video-dedup-local
.\.venv-ocr\Scripts\python.exe -m pip install -r requirements-web.txt
```

创建一个外部用户密钥：

```powershell
.\.venv-ocr\Scripts\python.exe -m web_gateway.cli create-key --label "测试用户" --maximum-active-jobs 3
```

完整密钥只会在创建时显示一次。启动本地网页：

```powershell
.\start_web_gateway.ps1
```

浏览器访问 <http://127.0.0.1:8788>；公网访问
<https://upload.andymori.uk>。网关只监听环回地址，不需要开放 Windows 防火墙端口。

密钥可以随时查看记录或禁用：

```powershell
.\.venv-ocr\Scripts\python.exe -m web_gateway.cli list-keys
.\.venv-ocr\Scripts\python.exe -m web_gateway.cli disable-key <key_id>
```

## 2. 工程目录与任务运行目录

用户选择/上传的剧集文件夹就是工程根目录。网页任务完成后，用户可见成果固定发布为：

```text
E:\wangyang\Videos\短剧输出\<剧名>\
├─ 字幕终稿\              标准化最终 SRT、实体表和字幕清单
├─ processed\             去重、字幕烧录后的普通成片
├─ 解说\                  解说项目最终成片及版本材料
└─ 任务记录\
   └─ <任务名>_<时间>_<job短ID>\
      ├─ logs\
      ├─ config\
      ├─ agent\
      └─ manifest.json
```

任务名是可读标签，真正的不可变身份仍是完整 job ID。任务记录目录同时包含任务名、
创建时间和 job 短 ID；`manifest.json` 保存完整 job ID、版本、工程根目录和发布文件
清单，因此任务改名或重名不会切断追踪关系。

当同一分类发布新结果时，网关只会把上一任务清单中明确登记的旧文件移入上一任务的
记录目录，然后发布新文件。无法确认来源的用户文件不会被移动或覆盖；同名时新文件
自动附加 job 短 ID。

每个任务仍保留独立的内部运行目录，用于断点、HTTP 下载和故障分析：

```text
E:\wangyang\Videos\短剧输出\<剧名>\.video-service\jobs\<任务ID>\
├─ input\                 上传并校验完成的源视频
├─ assets\                背景音乐、边框和动态特效素材
├─ config\                视频配置、任务设置和无密钥执行记录
├─ .chunks\               尚未合并的上传分片
├─ .secrets\              不通过下载接口暴露的兼容单任务令牌
├─ agent-runtime\         与本地 agent_bridge.py 相同的桥接运行目录
└─ result\
   ├─ videos\             发布前的不可变任务视频
   ├─ subtitles\          发布前的最终 SRT、字幕清单和实体表
   ├─ logs\               process.log 和全部翻译诊断记录
   ├─ agent\              规则、请求、检查点、响应和审核归档
   └─ manifest.json       任务版本及最终路径
```

SQLite 数据库位于：

```text
E:\wangyang\Videos\短剧输出\.video-service\gateway.sqlite3
```

同一剧名的任务在数据库和 `manifest.json` 中按 `v0001`、`v0002` 递增。内部任务
目录以不可变任务 ID 命名，避免同一剧同时创建版本时发生目录冲突；普通用户不需要
进入 `.video-service` 寻找最终成品。

## 3. 并发与排队

调度限制固定为：

- 同时运行的文件夹/剪辑任务：1；其余任务进入 SQLite 队列。
- 启用字幕时：同一文件夹最多同时准备/处理 3 个视频。
- 不启用字幕时：同一文件夹最多同时处理 10 个视频。
- 网页上传：示例页同时上传 3 个文件；每个文件按 32MB 分片。
- Agent 并行上限：3集，且不得超过当前文件夹实际集数。

队列是本机持久化队列。网页关闭不会删除任务；重新输入任务 ID 并重新选择相同
文件，可从服务器已确认的分片继续上传。

## 4. Agent 获得的材料

HTTP Agent 桥接复用 `agent_bridge.py` 的协议版本、完整性检查、时间戳检查、质量
门槛和响应校验。Codex领取任务时获得与本地模式相同的完整 `request.json`：

- 文件夹任务设置、目标语言、源语言和 OCR 语言；
- 字幕来源与本地化策略；
- OCR/软字幕和 Whisper ASR 对齐材料；
- 每集、每句的稳定索引及开始/结束时间；
- 置信度、分组信息和翻译契约；
- 术语表、已有实体表和高级模式历史终稿；
- 全剧审核、质量门槛和提交完整性规则。

此外，任务专用 artifacts 接口允许 Codex读取当前任务的：

- 实时 `process.log`；
- 翻译诊断 JSON/Markdown；
- `request.json`、已接受检查点和最终 `response.json`；
- 最终字幕、实体表和结果清单。

注册文件、控制文件、其他剧集和任意 Windows 路径不会通过接口暴露。

## 5. 主要 HTTP 接口

外部网页使用 `X-API-Key: vdl_...`：

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/v1/agent-session` | 获取可持续监听的账号级 Agent 初始化命令 |
| POST | `/api/v1/agent-session/rotate` | 替换 Agent 对话并使旧会话立即失效 |
| GET | `/api/v1/agent-session/status` | 查看初始化状态和最近监听心跳 |
| POST | `/api/v1/agent-session/stop` | 停止监听并使当前对话令牌失效 |
| GET | `/api/v1/settings-schema` | 获取完整桌面等价参数、预设、语言、字体和术语表 |
| POST | `/api/v1/jobs` | 创建批量任务和兼容单任务 Agent 令牌 |
| PUT | `/api/v1/jobs/{job}/uploads/{upload}/chunks/{index}` | 上传一个二进制分片 |
| POST | `/api/v1/jobs/{job}/uploads/{upload}/complete` | 合并并校验完整视频 |
| POST | `/api/v1/jobs/{job}/start` | 加入持久化队列 |
| GET | `/api/v1/jobs/{job}` | 状态、版本、队列位置和输出路径 |
| GET | `/api/v1/jobs/{job}/events` | 增量读取处理日志 |
| POST | `/api/v1/jobs/{job}/cancel` | 停止上传、排队、Agent和本地进程 |
| GET | `/api/v1/jobs/{job}/artifacts` | 列出最终视频、字幕和日志 |
| GET | `/api/v1/recap/voices` | 列出解说声纹 |
| POST/GET/PUT | `/api/v1/jobs/{job}/recap` | 新建、读取、更新版本化解说项目 |
| POST/PUT/DELETE | `/api/v1/jobs/{job}/recap/segments...` | 增删改解说片段 |
| POST | `/api/v1/jobs/{job}/recap/validate` | 校验源视频区间 |
| POST | `/api/v1/jobs/{job}/recap/actions` | 排队执行响度、局部、预览或最终渲染 |

创建任务时，`series_name` 是允许用户修改的任务名，`project_name` 是源文件夹名。
两者相同时可只传 `series_name`；网页会同时传入两者，保证修改任务名不会改变工程根目录。

Codex 默认使用账号级 `Authorization: Bearer agent_session_...`。一个对话初始化
后可以持续领取该账号后续任务；重新生成会话会让旧对话立即失效：

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/v1/agent/sessions/{key_id}/rules` | 读取与本地桥接同步的长期会话规则 |
| POST | `/api/v1/agent/sessions/{key_id}/listen` | 持续轮询该账号后续文件夹任务 |
| GET | `/api/v1/agent/jobs/{job}/rules` | 读取与本地桥接同步的规则 |
| POST | `/api/v1/agent/jobs/{job}/listen` | 轮询并领取OCR/ASR材料包 |
| POST | `/api/v1/agent/jobs/{job}/heartbeat` | 续租并检查取消 |
| POST | `/api/v1/agent/jobs/{job}/checkpoint` | 提交已完成集的检查点 |
| POST | `/api/v1/agent/jobs/{job}/submit` | 提交全剧最终结果并执行本地校验 |
| GET | `/api/v1/agent/jobs/{job}/artifacts` | 查看当前任务的日志与记录 |

生产服务关闭 `/docs` 与 OpenAPI 暴露；集成协议以本文和自动化测试为准。

## 6. 第三方 APP 的集成边界

对方可以直接复用 `web_gateway/static/index.html`，也可以只按照 OpenAPI 接口重写
页面。后端任务状态机、目录、分片协议和 Agent 协议不依赖 React、Vue 或其他前端
框架。

对方不应：

- 在浏览器代码中硬编码长期访问密钥；
- 把 Agent 令牌放进 URL 查询参数；
- 绕过 `/complete` 的大小和 SHA256 校验；
- 根据本地磁盘路径直接下载文件；
- 自行放宽字幕索引、时间轴和 8.5/9.5 质量门槛。

远程网页支持 Agent 与 API 两种字幕翻译模式。API Key 只写入当前任务的隔离密钥
文件，并在子进程启动后立即删除；不会进入 SQLite、配置回显或下载 artifacts。
第三方页面不得把 LLM API Key 写入 localStorage。

字幕随机帧预览完全在远程用户浏览器中使用 `<video>` 和 Canvas 解码；随机跳帧、
拖动/缩放字幕区域、蒙版、字号及位置预览不会上传画面。只有用户点击创建任务后，
源视频与完整设置才通过分片接口发送到服务器。

如果对方页面与网关不在同一个来源，必须通过 `-AllowedOrigins` 明确指定其 HTTPS
来源；默认不开放跨域调用。例如：

```powershell
.\start_web_gateway.ps1 -AllowedOrigins "https://app.example.com"
```

## 7. 将来接入固定域名

购买域名并创建 Cloudflare Named Tunnel 后，将公开地址传给启动脚本：

```powershell
.\start_web_gateway.ps1 `
  -HostAddress 127.0.0.1 `
  -PublicUrl "https://video.example.com"
```

Tunnel 本身只转发到 `127.0.0.1:8788`。不要直接开放 Windows 防火墙端口，也不要
使用每次重启会变化的 `trycloudflare.com` Quick Tunnel 作为正式地址。

## 8. 当前交付范围

已经实现桌面批处理参数的网页接口和页面、辅助素材上传、浏览器本地随机帧预览、
任务版本、分片、队列、停止、日志、Agent/API 翻译、解说项目版本和渲染操作。
公网健康检查、鉴权、页面加载及浏览器控制台已完成联调。自动过期清理目前未启用，
避免误删素材；后续维护命令只能清理 `.video-service/jobs`，不能删除剧名目录中的
原始视频。
