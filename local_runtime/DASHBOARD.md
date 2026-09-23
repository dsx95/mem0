# Knowin 记忆空间：网页总览与上传

网页读取现有 Qwen 配置、Qdrant 集合 `mem0_7a008ae9dff3`，覆盖该集合下的所有用户。
现有资料、视频和聊天记忆直接显示，不需要迁移或重新导入。页面与模型通信都由项目 uv 环境运行。

## 打开页面

服务器启动 / 查看 / 停止：

```bash
bash /Knowin/foundation/seb/mem0-main/local_runtime/dashboard.sh start
bash /Knowin/foundation/seb/mem0-main/local_runtime/dashboard.sh status
bash /Knowin/foundation/seb/mem0-main/local_runtime/dashboard.sh stop
```

`start` 可重复执行，后台进程会复用；服务器重启后需再次执行。日志在
`/Knowin/foundation/seb/mem0-main/.data/dashboard/server.log`。

Mac 上已提供一键启动和转发脚本：

```bash
bash /Users/dangsx/Documents/ChatGPT/volcengine/open_mem0_dashboard.sh
```

它启动远端网页服务，建立 SSH 转发，并打开 <http://127.0.0.1:18580>。
手动转发也可以：

```bash
ssh -N -L 127.0.0.1:18580:127.0.0.1:18580 speech-volcengine
```

此版本为单人管理页面，绑定服务器 `127.0.0.1`，通过本机或 SSH 转发访问；未开放公网，未做多人账号权限。
所有用户范围都由管理员可见，筛选用户不是权限隔离。需要多人使用时，应先增加登录与服务端授权。

## 页面能力

- **记忆总览**：查看总数、来源数、视频记忆数和用户范围；分页浏览所有记忆，按文档、图片、视频、对话、文本、结构数据分类。
- **检索**：关键词搜索文件名及内容，不调用模型；语义搜索使用现有 embedding，默认返回过滤范围内最相关的 20 条。
- **详情**：完整文字、用户、来源、页码或时间范围、去重标识、写入历史，以及原文件下载。图片可预览，视频可从对应片段开始播放；PDF 可在新窗口打开指定页。
- **资料库**：按来源查看片段数和状态，点击进入对应文件的记忆。也会展示已有 CLI 报告中的失败来源，例如不完整的 `X1.mp4`。
- **添加记忆**：多文件选择或拖入，支持直接输入文本；选择所属用户后上传。默认 `knowin_public`。
- **导入记录**：上传进度、排队、解析、写入片段进度、完成与失败原因，失败可重试。关闭浏览器后服务器继续处理；服务重启后恢复未完成任务。

界面会每 5 秒刷新任务状态和统计。任务完成后刷新记忆总览；浏览器关闭不会中止已经传到服务器的文件。

## 支持格式和限制

| 格式 | 处理方式 |
| --- | --- |
| PDF | 逐页文字提取，必要时补充视觉解析；保留物理页码 |
| DOCX | 优先复用已有固定 PDF；新文件在无 LibreOffice 时提取正文、表格、内嵌图片，不伪造页码 |
| PNG、JPG、JPEG、GIF | 复用现有视觉解析和 GIF 抽帧逻辑 |
| MP4、MOV、MKV、AVI、WebM、M4V | 复用视频抽帧与音轨转写；是否可解码取决于实际文件 |
| TXT、Markdown、HTML | 提取文字；HTML 脚本不会作为网页执行 |
| JSON、CSV | 作为资料文字分块保存；JSON 校验语法并格式化，不自动创建家庭成员/地图实体数据库 |

单文件最多 512 MB，单次最多选择 20 个文件；JSON/CSV 最多 8 MB；单视频最多 30 分钟。
单次文件导入最多 2000 条片段。旧版 `.doc` 请先转为 DOCX/PDF；暂不支持 PPTX、XLSX、压缩包或独立音频上传。
Word 需要精确页码时请上传 PDF。音轨使用片段时间范围，不是逐字对齐；视频画面是采样理解，可能遗漏瞬间动作。

模型继续使用 `qwen-vl-plus`、`qwen3-asr-flash` 和 1024 维 `text-embedding-v4`。
API Key 只在服务器配置中读取，不传给网页。新增媒体解析和语义检索会调用当前配置的模型。

## 存储与防重复

记忆仍存于项目 `.data/qdrant/` 和 `.data/mem0_7a008ae9dff3_history.db`。
新增 `.data/dashboard/jobs.sqlite` 仅保存网页导入任务，不替代记忆数据库。
网页新上传的原文件保存在：

```text
/Knowin/foundation/seb/material/诺因公开资料/网页上传/<文件 SHA256>/<原文件名>
```

上传时计算 SHA-256。同一用户、同一资料根目录中，若已有相同内容且原文件仍可用，
会复用已有来源路径，因此把文件改名后再上传也能跳过已入库片段。
随后按原 `ingest_key` 逻辑检查数据库，补齐缺失片段；文件内容改变则保留新旧版本。
这不是跨文件语义去重，也不会自动解决冲突信息。

解析缓存仍在 `.data/materials/`，网页写入与 CLI 共享导入报告。
**网页服务持有本地 Qdrant 的进程锁。** 使用现有 `local_runtime` 或 `materials` CLI 读写记忆前，
先停止网页服务；执行完再启动。Agent 若与网页同时使用，可通过下述本机 HTTP 检索接口访问。
该版本不支持两个独立 Memory 进程同时打开同一本地 Qdrant。

## 本机接口

| 接口 | 用途 |
| --- | --- |
| `GET /api/overview` | 总量、分类、用户、模型名称；不返回凭据 |
| `GET /api/memories?page=1&page_size=12&user_id=…&kind=…&q=…` | 浏览与关键词筛选 |
| `GET /api/search?q=…&user_id=…&kind=…&limit=20` | 语义检索；可增加 `source` 按来源筛选 |
| `GET /api/memories/{id}` | 详情与历史 |
| `GET /api/memories/{id}/file` | 原文件；支持 `asset=preview`、`asset=audio` 和 `download=true` |
| `GET /api/sources` | 来源清单 |
| `GET /api/jobs` | 网页导入任务 |
| `POST /api/uploads?filename=…&user_id=…` | 请求体为文件原始字节，返回已排队的任务 |
| `POST /api/jobs/{id}/retry` | 重试失败任务 |

写接口要求 `X-Memory-Client: dashboard` 请求头；浏览器写请求还校验同源。
这只是本机请求约束，不是多人身份认证。HTML 原文件强制下载，文件读取只允许资料根目录与解析缓存内的已关联文件。

## 验证记录（2026-09-19）

- 12 项网页后台测试通过：分页、用户筛选、语义范围、重复与改名上传、版本区别、失败重试、大小和路径校验、跨站请求、Word 无页码处理、任务恢复。
- 原有 17 项 materials/video 测试继续通过。Ruff、格式检查、依赖检查通过。
- 真实浏览器验证 201 条原有记忆的分页、分类和详情；视频 HTTP Range 返回 206，并从 10 秒处开始正常播放。
- 在独立测试用户下上传 TXT、DOCX、两页 PDF、JSON、CSV，共写入 6 条测试记忆；PDF 页码保留，DOCX 明确无页码。相同文本重传新增 0 条。
- 把已有图片改名上传，新增 0 条、跳过 7 条；已有视频通过上传接口重传，新增 0 条、跳过 2 条。
- 错误 JSON 正确显示为失败；桌面和 390px 移动端页面均验证，无横向溢出、无 JavaScript 运行错误。
- 6 条测试记忆、6 份测试上传原文件与 9 条测试任务已精确清理；原 201 条记忆的存储内容逐条一致。备份位于 `.data/setup-backups/20260919-dashboard/`。

服务生命周期沿用 [FastAPI 官方的 lifespan 方式](https://fastapi.tiangolo.com/advanced/events/)，统一管理一个常驻 Memory。
