# 安装与验证记录

日期：2026-09-15。主机：`speech-volcengine`。

- 项目：`/Knowin/foundation/seb/mem0-main`
- 解释器：`/Knowin/foundation/seb/mem0-main/.venv/bin/python`
- Python：3.11.4；`sys.prefix != sys.base_prefix`，确认是独立虚拟环境。
- SDK：`mem0ai 2.0.20`，editable 安装；导入文件是项目内 `mem0/__init__.py`。
- uv：0.12.6；安装 43 个包；`.venv` 约 172 MB。
- 主要依赖：openai 3.14.0、ollama 0.6.2、qdrant-client 1.19.0、python-dotenv 1.2.3。
- 锁文件：`local_runtime/requirements.lock`。`UV_OFFLINE=1 bash local_runtime/setup_uv.sh` 通过，证明当前缓存可离线同步已锁定环境。
- 根目录 `.env` 权限：0600。仅放配置模板，没有真实密钥；`config` 正确报告尚未补齐模型配置。
- `.gitignore` 新增环境、uv 缓存、记忆数据及环境配置的忽略规则；原内容备份为 `local_runtime/gitignore.before-20260915.txt`。

验证结果：

```text
uv pip check --python .venv/bin/python
Checked 43 packages
All installed packages are compatible

.venv/bin/ruff check local_runtime
All checks passed!

.venv/bin/ruff format --check local_runtime
5 files already formatted

.venv/bin/python -m pytest local_runtime/test_runtime.py -q
15 passed in 34.33s

bash -n local_runtime/setup_uv.sh
exit 0
```

测试使用本机回环 HTTP 模型服务、真实 provider 代码、真实 Qdrant 和 SQLite。涵盖 OpenAI 兼容调用、Ollama 原生调用、混合 LLM/Embedding、独立地址/Key、维度检查、401 错误、脱敏配置、缺模型处理、跨 Python 进程持久化及用户隔离。

核心文件在安装前后校验一致：

```text
pyproject.toml
0b63d72960057d1c86d6c9e59417688fe75485384e1a08f0257a2e0b38b61fa3
mem0/memory/main.py
5b1b75e2f00aca7bd368a6e9cd5905145d60fd05a0e36d6b1ef3e2f1b4f28ca1
mem0/llms/openai.py
37e6d3c22b55df0731740f4e0868b95bd480ce44363227fe53461db328c5f67e
mem0/llms/ollama.py
72b0f52b7d99dbfa854bbb1a8c706fd56396e53da89b5c75c439db599a2cf2c5
mem0/embeddings/openai.py
bd8c0cfeaa1ac89511350d51c5042a3b01f90530e18259ed5b0c3e019595e1e1
```

真实外部模型尚未联调：没有使用真实模型 Key。没有下载推理权重，也没有启动推理服务器。

## 2026-09-15：OpenAI / Qwen 单 Key 配置

已创建权限 `0600` 的 `local_runtime/openai.env` 和 `local_runtime/qwen.env`，每份仅需填写一次 `MEM0_PROVIDER_API_KEY`。固定预设分别为 OpenAI `gpt-4.1-mini` + `text-embedding-3-small`（1536 维），以及百炼北京地域 `qwen-plus` + `text-embedding-v4`（1024 维）。两个文件目前均为空 Key，脱敏 `config` 正确显示 `configured: false`。

```text
.venv/bin/python -m pytest -q -o addopts= local_runtime/test_runtime.py
20 passed in 37.77s

.venv/bin/ruff check local_runtime
All checks passed!
```

新增验证：同一 Key 传给两个模型、预设防止旧环境变量混入其他平台、Qwen LLM 请求包含 `enable_thinking=false`、25 条 embedding 输入分成 10/10/5、空批次不发送请求，以及两种预设通过真实 Mem0 add/search 调用链。这些请求全部发往测试回环服务，未调用云端模型。

原有根目录 `.env` 的内容校验保持一致。修改前的三个运行层文件备份位于 `.data/setup-backups/20260915-single-key-profiles/`。真实服务测试在填入 Key 后执行：

```bash
cd /Knowin/foundation/seb/mem0-main
.venv/bin/python -m local_runtime --env-file local_runtime/openai.env check
.venv/bin/python -m local_runtime --env-file local_runtime/qwen.env check
```

## 2026-09-16：spaCy 与 BM25 安装完成

安装目标仍是 `/Knowin/foundation/seb/mem0-main/.venv`。本次新增 41 个包，共 84 个包，`.venv` 约 492 MB。原有 OpenAI、Ollama、Qdrant、NumPy 等版本保留，系统和 Conda 环境未修改。

| 组件 | 已安装版本 |
|---|---|
| spaCy | 3.8.16 |
| en_core_web_sm | 3.8.0 |
| fastembed | 0.8.0 |
| onnxruntime（CPU） | 1.30.0 |

`requirements.in` 使用本地 editable `.[nlp]`，单独加入 fastembed 与 spaCy 模型 wheel；更新后的 `requirements.lock` 已同步。验证 `UV_OFFLINE=1 bash local_runtime/setup_uv.sh` 成功，因此环境同步不会移除这些依赖。

运行验证：

- spaCy lemma 与 full 两条加载路径均成功，未再出现缺少 spaCy 的提示。
- 使用实际英文句子验证词形还原，`lattes` 得到 `latte`；实体抽取识别 `Alice` 和 `London`。
- 在临时 Qdrant 数据库写入两条测试记录，BM25 查询命中对应记录，换用户返回空列表；测试后临时数据库自动清理。
- 上述 NLP/BM25 验证使用 `HF_HUB_OFFLINE=1`，没有云端模型调用。
- `HF_HUB_OFFLINE=1 .venv/bin/python -m pytest -q -o addopts= local_runtime/test_runtime.py`：**20 passed in 35.44s**。
- Ruff 检查、格式检查、`uv pip check` 均通过。
- 实际运行原来的 Qwen 查询，`demo_user_001` 仍能找回“无糖拿铁、不吃香菜”，且三条缺依赖提示全部消失。本次真实查询没有写入或修改旧记忆。
- 根目录 `.env`、`local_runtime/openai.env`、`local_runtime/qwen.env` 的内容校验均保持一致。

BM25 首次从服务器访问 Hugging Face 时出现 TLS EOF。已从官方 `Qdrant/bm25` 仓库固定修订 `22b8d2af71a76161e18dd432d2cee0eefa66e412` 获取 31 份辅助资源（合计 81,436 字节），逐文件核对官方 Git blob 哈希后传入项目缓存。资源位于 `.data/models/fastembed`，下载记录为该目录中的 `download-manifest.json`。`local_runtime` 现在默认使用此持久缓存，已有 `FASTEMBED_CACHE_PATH` 环境变量仍优先。

已有记忆未回填 BM25 稀疏向量或实体索引；后续新写入记录会走完整的新依赖路径。spaCy 按当前源码加载英文模型，中文语义检索仍由 Qwen embedding 提供。

安装前依赖文件、运行层文件与配置校验记录保存在 `.data/setup-backups/20260916-nlp-bm25/`，没有复制真实 API Key。


## 2026-09-17：诺因公开资料导入

新增 `local_runtime/materials.py`、`test_materials.py`、`MATERIALS.md` 和逐文件报告 `MATERIALS_IMPORT_20260917.md`。

- 46 份有效文件全部完成，共 180 条资料片段；忽略 4 个 `.DS_Store`。类型为 Word 5、PDF 1、PNG 36、JPG 1、GIF 1、HTML 1、TXT 1。
- 使用现有 Qwen 配置和同一 Key：视觉 `qwen-vl-plus`，向量 `text-embedding-v4`（1024 维）。`infer=False` 写入，不经过事实概括。资料范围 `user_id=knowin_public`。
- 在项目 uv 环境加入 PyMuPDF 1.28.2、BeautifulSoup4 4.15.0、soupsieve 2.9.2；已有 Pillow 12.3.0。共 87 包，原有核心依赖版本保留。更新锁文件，标准 `UV_OFFLINE=1 bash local_runtime/setup_uv.sh` 成功。
- 服务器没有 LibreOffice。此次 5 份 Word 在本机通过临时 CJK 字体配置转换为固定 PDF，再上传带源/PDF 哈希的分页缓存；合计 25 页，各文件原 XML 与 PDF 提取的中文字符数一致，全部页面联系表已视觉检查。该页码不保证等于其他机器 Word 的排版页码。原 PDF 另有 22 页，47 个物理页全部有记录。
- 37 张静态图片已解析；GIF 共 123 帧，采样 12 帧，保存真实时间戳。未宣称完整逐帧理解。102 份成功视觉响应已缓存；缓存记录合计输入 123673、输出 41321 tokens，合计 164994（此数不包含 embedding 用量，也不是账单金额）。
- 逐条核验 Qdrant、报告中的 180 个 ID 与 SQLite 历史一致；片段标识无重复；全部原件哈希不变。
- 全量重跑：46 份完成，新增 0 条，视觉调用 0 次。数据备份在 `.data/setup-backups/20260917-materials/`；原 `.env`、`openai.env`、`qwen.env` 校验均未变化。
- 真实检索融资资料、产品参数、GLOW 架构通过；不同用户范围无结果，原 demo 用户记忆仍可检索。
- 产品参数的中英混合查询在原混合排序中优先命中功能介绍，纯语义检索将包含 40 cm、23 个自由度、6 kg 的第 7 页参数表排在第一。因此 `materials search` 默认语义检索，并按用户、根目录和可选文件过滤；`--hybrid` 可使用现有 Mem0 排序。没有修改原 `Memory.search()` 或 `local_runtime search`。

测试与命令验证：

```text
HF_HUB_OFFLINE=1 .venv/bin/python -m pytest -q -o addopts= local_runtime/test_runtime.py local_runtime/test_materials.py
27 passed in 138.62s

# 最后调整资料检索入口后再次验证导入器
HF_HUB_OFFLINE=1 .venv/bin/python -m pytest -q -o addopts= local_runtime/test_materials.py
7 passed in 0.97s

.venv/bin/ruff check local_runtime
All checks passed!

.venv/bin/ruff format --check local_runtime
All files already formatted

uv pip check --python .venv/bin/python
Checked 87 packages
All installed packages are compatible
```

机器校验：`.data/materials/verification-20260917.json`；实际 CLI 检索输出：`.data/materials/cli-search-20260917.json`。
完整日志：`.data/materials/ingest-20260917-parallel.log`、`repeat-20260917.log`、`setup-repeat-20260917.log`。

限制：图片转文字可能存在识别误差；跨文件重复内容仍保留独立来源；源文件变更会追加新版本而不自动删除旧记录；新增 Word 需提供固定 PDF 缓存或服务器可用的 LibreOffice，见 `MATERIALS.md`。

## 2026-09-17：检索耗时定位与进程复用

实测未开启性能分析器的独立 CLI：原混合查询 31.786 秒，资料语义查询 23.855 秒。主要为重复 Python/SDK 导入、初始化和首次 spaCy 加载；本地向量查询约 11 ms。单独拆分首次 embedding：共 5.657 秒，其中 SDK resource 模块加载 5.484 秒，HTTP 请求 0.151 秒。

新增 `materials shell --timings`，同进程持有一个 Memory，复用 HTTP 客户端，每次重新查询。语义模式后续 5 次不同/重复问题的中位数 130.70 ms（108.83–705.69 ms）；混合模式 260.69 ms（131.70–291.56 ms）。首次启动与首次查询仍有冷启动成本。首尾查询 ID 与各自原命令相同。

10 项材料模块测试通过（含原 7 项）；真实 shell 两种模式各 6 条查询，以及单次 search 计时入口通过。Ruff 和格式检查通过。原 182 条记忆及 182 行 history 保持，runtime.py 和三个 env 文件校验未变，无新增依赖，无驻留服务。备份在 `.data/setup-backups/20260917-latency/`，阶段记录在 `.data/latency/`。完整结论和 Agent 接入方式见 `LATENCY.md`。

## 2026-09-18：视频资料导入

新增视频解码、分段画面解析和音轨转写。项目 uv 环境新增 PyAV 16.1.0，共 88 包；现有 Key 实测可调用 `qwen-vl-plus`、`qwen3-asr-flash`，原 embedding 和配置文件保持。

4 段 MP4 中 3 段正常完成，共新增 19 条有效视频记忆（17 条画面、2 条音轨）。`X1.mp4` 文件不完整、缺少 MP4 索引，未导入。原有 182 条记录内容逐条核对不变，当前 Qdrant 共 201 条。清理了本次首次写入的 2 条纯音乐符号记录，历史保留 203 次 ADD、2 次 DELETE。

真实检索三份视频和用户隔离通过；重复导入新增 0 条，视觉/ASR 调用 0 次。20 项 runtime、17 项 materials/video 测试、Ruff、离线 uv 同步及依赖检查全部通过。完整结果与限制见 `MATERIALS_VIDEO_IMPORT_20260918.md`，使用方式见 `VIDEOS.md`。

## 2026-09-19：网页记忆总览与文件上传

新增本机 FastAPI 网页控制台，复用现有 Qwen 配置、同一记忆集合与资料解析缓存。可浏览全部用户的记忆，关键词或语义搜索，查看来源、页码、视频时间、原文件与历史；多文件上传、直接输入文本、后台队列与失败重试。

环境新增 FastAPI 0.141.1、Starlette 1.6.0、Uvicorn 0.53.0，共 91 包；原 86 项普通版本锁不变。离线标准 uv 同步和依赖检查通过。未更改系统/Conda 环境、现有三个 env 文件或 runtime.py。

12 项 dashboard 测试与原 17 项 materials/video 测试一起通过（29 passed）。Ruff、格式检查通过。Starlette TestClient 对 AnyIO 的旧类型别名产生一项 DeprecationWarning，不影响测试及实际运行。

真实 Chrome 浏览器验证：201 条原有记忆、49 份已入库资料；分页、分类、用户筛选、语义搜索、来源跳转、详情均通过。视频支持 Range 206，并实测从第 10 秒播放至第 11 秒。1440px 桌面和 390px 移动端无横向溢出、无 JavaScript 运行错误。

在独立用户下上传 TXT、DOCX、两页 PDF、JSON、CSV，成功写入 6 条；文本重传新增 0 条。已有图片改名上传跳过 7 条，已有视频上传跳过 2 条。错误 JSON 显示失败并可重试。随后精确清理 6 条测试记忆、6 份测试原文件和 9 条测试任务；Qdrant 中原 201 条记录的存储内容与此次备份逐条一致，原配置哈希一致。

服务停止/重启及重复 start 已验证：复用同一 PID，恢复后仍显示 201 条。网页服务默认仅监听服务器 127.0.0.1:18580，经 SSH 转发到本机访问；不是多人权限系统。详细启动、支持格式和 Qdrant 进程锁限制见 `DASHBOARD.md`。

备份：`.data/setup-backups/20260919-dashboard/`。机器核验：`.data/dashboard/verification-20260919/`，包含浏览器结果、上传任务证据、测试数据清理前记录、数据完整性校验和依赖记录。

## 2026-10-01 — 待办人员、执行结果和提醒计划字段

- 扩展 TodoData、HTTP API、聊天 todo 工具和网页表单：发起人绑定登录身份，执行人与协作成员校验归属；补充所需资源、完成标准、受阻原因、完成说明、实际耗时、提醒计划开关/时间/失效时间/内容。
- actual_started_at 与 completed_by 由服务端记录。旧 JSON 补默认值，旧历史缺失值保持空，不虚构已发生的执行记录；原 ID、revision 与历史时间保持不变。
- todo members 只读当前家庭成员（个人空间只读本人），协作身份不额外授予编辑权；成员退出同时解除共享待办指派与协作，含回收站。
- 提醒窗口与截止时间独立，统一校验带时区时间与先后顺序；reminder_state 仅表示计划状态，reminder_delivery=not_configured。本次没有接入真实响铃、调度投递、外部通知或周期任务。
- 迁移导出 v5，接受 v1–v5；完整字段、时间与操作历史完成导出/恢复回归。
- 236 项回归通过（6 条既有第三方弃用警告）；新增字段测试 19 项通过。Ruff、变更 JS 语法与 git diff 检查通过。使用假模型验证工具协议，未调用付费 LLM。
- 隔离预览数据库完成浏览器创建/回显、切换执行人、执行记录编辑、权限锁定、提醒状态展示及布局检查。网页修改其他字段后，API 原有提醒时间的秒精度仍保留。
- 实际本地数据升级前备份 5 个 SQLite 文件至 migrations/backups/todo-details-upgrade-20260930T170721Z；升级后对比 chat.sqlite 原有 21 张业务表（排除登录令牌表），旧列逐行一致。当前 13 个用户、36 条已完成对话，未写入测试待办。
- 本地服务以 bash start.sh --port 18582 启动并验证 HTTP 200，实际网页已加载新表单；临时预览服务关闭。
