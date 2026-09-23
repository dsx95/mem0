# 诺因公开资料逐文件导入

导入入口为 `python -m local_runtime.materials`。默认读取
`/Knowin/foundation/seb/material/诺因公开资料`，使用 `local_runtime/qwen.env`，
资料范围为 `user_id=knowin_public`。现有聊天测试用户的数据继续保留。

## 日常命令

在服务器执行：

```bash
cd /Knowin/foundation/seb/mem0-main
source .venv/bin/activate

# 清点文件；不会调用模型或写入记忆。
python -m local_runtime.materials scan

# 只导入一份，参数为资料根目录下的相对路径。
python -m local_runtime.materials ingest \
  --file "诺因智能官方资料20260917.docx"

# 逐文件导入全部资料。重复执行会复用解析缓存，并跳过已存在的片段。
python -m local_runtime.materials ingest

# 查看每份文件的完成状态、片段数和错误类型。
python -m local_runtime.materials status

# 检索这个资料范围；返回结果包含来源、页码等 metadata。
python -m local_runtime.materials search \
  --query "Knowin-X1的收纳高度和最大负载是多少？" --top-k 5

# 连续查询推荐此入口：只初始化一次，随后直接输入问题；/exit 或 EOF 退出。
python -m local_runtime.materials shell --timings

# 只在指定文件内检索。
python -m local_runtime.materials search \
  --file "诺因智能官方资料20260917.docx" \
  --query "Knowin-X1的收纳高度、全身自由度和双臂最大负载是多少？"
```

可重复传 `--file` 选择几份文件。其他参数：`--root` 指定另一资料目录，
`--env-file` 指定配置，`--user-id` 指定资料范围，`--vision-model` 指定视觉模型。
当前验证的是同一百炼 Key 和北京兼容接口上的 `qwen-vl-plus`；普通记忆 LLM
仍为 `qwen-plus`，向量模型仍为 1024 维 `text-embedding-v4`。

资料入口 `materials search` 默认使用同一 Mem0 向量库的语义检索，并按用户、资料根目录和可选文件过滤。
实际验证发现，当前 Mem0 的英文 BM25 会使带 `Knowin-X1` 的中文参数问题优先命中反复出现型号的介绍页；
单纯语义检索能将真实参数表排到首位。需要对比原有排序时增加 `--hybrid`。
现有 `python -m local_runtime ... search` 和 `memory.search()` 的混合排序没有修改。

`shell` 和 `search` 支持相同的 `--root`、`--user-id`、`--file`、`--top-k`、`--hybrid` 过滤和排序参数。
`--timings` 会额外输出初始化耗时和本次查询耗时（毫秒）。首次启动和首次调用仍有冷启动开销；
后续查询复用同一个 Memory 实例和模型 HTTP 连接，不缓存查询结果，每次都会重新检索当前数据库。
交互进程占用本地 Qdrant，先输入 `/exit` 退出，再另开进程导入资料。耗时测量见 `LATENCY.md`。

`--vision-workers 3` 默认提前解析三份独立图片。PDF 解析和 Mem0 数据写入仍在主线程顺序执行。
视频支持 MP4 / MOV / MKV / AVI / WebM / M4V，以实际解码能力为准。默认每秒采样 1 帧、
每 10 秒生成一段画面记录，同时转写第一条音轨。具体使用和限制见 `VIDEOS.md`。GIF 按真实帧时长记录时间，
均匀抽取最多 12 帧，不能视为逐帧完整视频理解。

## 资料怎么被保存

每个文本片段通过 `memory.add(text, user_id=..., infer=False, metadata=...)` 写入。
这会生成文本向量并持久化资料原文，不让记忆抽取 LLM 再次概括而丢失技术细节。
图片先经视觉模型识别成文字；识别结果仍可能有误，需要准确引用时应回看原图。

| 来源 | 解析方法 | 定位字段 |
| --- | --- | --- |
| PDF | 逐页提取文本；有图片、图形或文字不足的页面另作视觉解析 | `page_start`、`page_end`、`page_count` |
| DOC / DOCX | 先生成固定 PDF，再逐页解析 | 同上；`rendered_file` 指向固定版 PDF |
| TXT / Markdown | UTF-8 原文分块 | `char_start`、`char_end` |
| HTML | 提取静态 DOM 文字，去掉脚本、样式 | 字符范围，不伪造页码 |
| PNG / JPG | 视觉文字识别及图示说明；长图按原分辨率切块 | `image_bbox`、`tile_index`、`preview_file` |
| GIF | 最多 12 个均匀采样帧，分别解析 | `frame_index`、`start_seconds`、`end_seconds` |
| 视频画面 | 每段按时间排列的采样帧，经视觉模型解析 | `start_seconds`、`end_seconds`、`sampled_frame_times` |
| 视频音轨 | 第一条音轨转为 16 kHz 单声道 WAV，每 30 秒转写，窗口重叠 1 秒 | `start_seconds`、`end_seconds`、`audio_file`；时间范围不是逐字对齐 |

所有记录还有 `source_file`（相对路径）、`source_path`（原文件路径）、
`source_sha256`（内容版本）、`doc_id`（来源标识）、`ingest_key`（片段标识）和 `extraction_method`。
每片段最多约 1400 个字符，相邻片段重叠 120 字符；按页、图块、帧切分，不将不同页混成一个页码。
原生 PDF 文本与视觉补充信息分别保留，并标明解析方式。

Word 页码是固定版 PDF 的物理页序，从 1 开始，不保证与其他机器上的 Word 排版相同。
`page_label` 保存 PDF 提供的页面标签；没有标签时使用实际页序。文字、图示的结构可能被简化，
精确表格、二维码及空间关系仍应核对原件。

此次 5 份 Word 已在本机通过带 Noto Sans CJK SC 的 LibreOffice 渲染，固定版 PDF 和
源文件/PDF 校验值已放入服务器缓存。源码目录里的原始文件没有修改。

新增 Word 文件时，导入器会在服务器寻找 `soffice` / `libreoffice`。该工具是外部程序，
不是 Python 包。当前服务器没有安装它；需要先在有 LibreOffice 的机器渲染，并将匹配的
`document.pdf`、`render.json` 放进 `.data/materials/rendered/<源文件SHA256>/`。
`render.json` 至少包含 `source_sha256`、`pdf_sha256`；两者必须与实际文件匹配。
已有这 5 份 Word 的日常重复导入无需重新转换。

## 防重复和中断恢复

片段标识由解析版本、来源路径、文件内容哈希、视觉模型、片段位置和文本生成。
导入器启动时分页读取 Qdrant 中同一用户和资料目录已有的 `ingest_key`，已存在的片段不再写入。
因此即使进程在写入成功后、报告保存前中断，再运行仍能识别已写入的片段。

解析结果和单张图的识别结果也会缓存，不会因为重新执行命令就再次调用视觉模型。
修改文件会形成新版本；当前不会自动删除旧版本或合并不同文件的相似内容。
不同日期资料中的参数、团队人数等可能不同，使用时应根据来源版本进行判断。

单文件失败会记录并继续其他文件，最终返回非零退出码；认证失败会停止整批调用。
上游错误只记录类型和状态码，不打印 API Key 或接口原始错误正文。
导入时本地 Qdrant 文件由导入进程占用，完成后再运行独立的 `search` 进程。

## 数据与缓存路径

以当前 Qwen 配置为例：

| 路径（均在项目根目录下） | 用途 |
| --- | --- |
| `.data/qdrant/` | 实际 Mem0 记忆和向量，集合 `mem0_7a008ae9dff3` |
| `.data/mem0_7a008ae9dff3_history.db` | Mem0 写入历史 |
| `.data/materials/import-69d928111fea79b5231b.json` | 此次根目录、集合、用户组合的导入报告 |
| `.data/materials/parsed/` | 按文件内容版本保存的解析结果 |
| `.data/materials/vision/` | 图片识别结果和调用用量 |
| `.data/materials/images/` | 实际送去识别的页面、长图局部和 GIF 帧 |
| `.data/materials/rendered/` | 固定版 Word PDF 与校验清单 |
| `.data/materials/videos/` | 视频采样帧、音频窗口及真实时间戳 |
| `.data/materials/video-responses/` | 视频画面解析、语音转写响应缓存 |
| `.data/materials/source-inventory-20260917.json` | 导入前原件清单和哈希 |
| `.data/setup-backups/20260917-materials/` | 导入前配置校验、依赖清单和数据库备份 |

原件仍位于 `/Knowin/foundation/seb/material/诺因公开资料`。备份时同时保留原件、
Qdrant、历史数据库和 materials 目录，才能完整追溯来源。

## 验证

```bash
cd /Knowin/foundation/seb/mem0-main
HF_HUB_OFFLINE=1 .venv/bin/python -m pytest \
  local_runtime/test_runtime.py local_runtime/test_materials.py local_runtime/test_videos.py -q
.venv/bin/ruff check local_runtime
.venv/bin/ruff format --check local_runtime
uv pip check --python .venv/bin/python
```

离线测试覆盖页码、分块覆盖范围、长图边缘、GIF 时间戳、来源版本及中断恢复去重。
真实导入和检索结果见 `VERIFICATION.md` 的资料导入记录。
