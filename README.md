# Knowin Memory — 独立 uv 版

Linux / macOS，Python 3.11。无需 Docker、PRoot、Conda、独立 Qdrant 或 root 权限。
代码基于本机保存的 2026-09-21 Mem0 核心及 2026-09-22 Coder 网页代码；未宣称与当前远端同步。

## 启动

```bash
git clone https://github.com/dsx95/mem0.git
cd mem0
bash start.sh
```

已下载或解压项目时，在项目目录直接执行 `bash start.sh`。

首次自动安装用户级 uv、下载 Python 3.11、创建项目 `.venv` 并安装锁定依赖。
随后在终端输入百炼北京地域 API Key（隐藏输入），写入权限 600 的 `config/qwen.env`。
默认 LLM / embedding 沿用原 Qwen 预设；联网模型调用仍需有效的 Key。
后续同一命令直接启动。打开 http://127.0.0.1:18580，聊天页为 /chat；Ctrl+C 停止。
Coder 请通过工作区私有端口转发访问 18580。默认仅监听回环地址，网页不是多租户鉴权服务。

聊天回答的通用行为写在 `local_runtime/chat.py` 的 `SYSTEM`；基于检索结果生成简短回答的提示词写在
`local_runtime/grounding.py` 的 `EXTRACT_SYSTEM`。每轮普通对话会先检索当前用户的个人记忆、当前家庭的共享记忆，
以及已启用且当前身份可见的内置资料，再交给 LLM；需要原文核验的问题会强制调用只读检索工具，覆盖相同的授权范围，
再让 LLM 合并重复内容并给每条陈述附上原文依据。`checked_answer()` 会核对引用 ID、原文片段和回答中的数字、
单位、网址后才向网页发送回答；这能拦截虚构来源和明显的数值错误，但不能从数学上证明每句概括都与原文语义一致。

同一 `family_id` 的成员共用家庭记忆键；个人记忆键由 `(family_id, user_id)` 生成。保存时由输入区的
`remember_scope` 决定写入个人还是家庭；普通聊天和需核验的问答都会读取当前个人、当前家庭及已启用的内置资料。
最近 12 个已完成对话轮次（最多 32000 字符）作为短期上下文；完整对话保存在 `chat.sqlite`，但不会自动
全部转成 Mem0 向量记忆。长期事实只有在模型实际调用 `remember` 且写入成功后才保存。Mem0 新记录自带 UTC
`created_at`、`updated_at`，页面按本地时区显示完整时间；旧记录缺失时显示「时间未知」，不会伪造时间。

### 用户、家庭、设备与记忆管理

首页现在是用户自己的记忆管理页。先选择或创建用户，再在「家庭与设备」中建立家庭、添加成员、登记设备。
一个用户可以加入多个家庭、使用多台设备；家庭设备由家庭创建者登记，也支持用户自己的个人设备。
切换用户后，家庭/设备下拉选项和记录会按服务端关系表重新加载。聊天页也可选择相同的用户、家庭和设备。

| 记录 | 可见范围 | 可执行的管理操作 |
|---|---|---|
| 个人内置资料、个人长期记忆 | 仅所属用户 | 查看、搜索、隐藏/恢复、删除；添加文本或上传资料 |
| 家庭共享资料、家庭长期记忆 | 家庭成员 | 成员可查看、对自己隐藏；记录创建者或家庭创建者可删除 |
| 公共内置资料 | 所有已选用户 | 查看、对自己隐藏/恢复，不允许普通用户删除公共原件 |
| 短期与完整对话 | 仅对话所属用户 | 按家庭/设备筛选、继续对话、删除一轮或整段 |
| 每日记事 | 仅日记所属用户 | 按日期/家庭/设备查看、删除筛选范围内当天记录 |

以 **user_id 为私有权限边界**，family_id 表示共享关系，device_id 表示来源/使用设备。共享一台设备不会共享
私人对话、日记或个人偏好。长期个人记忆仍按 `(family_id, user_id)` 汇总，同一家庭内切换设备可以沿用偏好；
device_id 不把个人事实拆成多个互不相通的向量库。管理页按设备过滤来源。聊天读取内置资料时，选择设备会读取
公共/未标记设备资料和当前设备资料；不选设备时可以检索当前身份可见的各设备资料。

删除长期记忆会清理 Qdrant 记录、该条修改历史、工具事件和日记中的新增记忆副本，并使相关摘要失效。
删除对话/单轮会同时清理对应日记副本；删除某天日记会清理对应原始轮次，防止重启回填。
这些操作不会自动删除独立的长期事实或资料源文件，源文件、解析缓存和备份目前保留，需要单独制定清理策略。
「对我隐藏」只影响该用户浏览和后续检索，不影响其他家庭成员；原始对话中已经出现的内容仍可另行删除。

身份、成员和设备关系存在 `data/dashboard/chat.sqlite` 的 `app_users / app_families / app_members / app_devices`；
临时登录令牌只存哈希，隐藏记录和删除重试标记也存在同一个库。会话和消息上下文新增 device_id，向量元数据同步记录。
不需要新增数据库服务或 Redis。升级时自动补字段，并一次性导入旧用户/家庭关系；旧记录标为「未标记设备」。
首次迁移的旧家庭以首先导入的成员为创建者，恢复后请核对现有关系。停机备份仍应覆盖完整 data、materials、config。

按当前要求提供**无密码用户切换模式**。服务端用 HttpOnly Cookie 绑定所选身份，所有数据接口做归属校验；
但任何能打开本机页面的人仍然可以选择另一个用户，因此这不是正式登录认证，不能直接开放给互不信任的用户。
后续可用真实登录替换身份选择入口，沿用服务端权限校验。多标签页身份变化时，旧页面请求会被拒绝，需重新选择身份。

API 接入和删除边界见 [用户与设备接口](local_runtime/IDENTITY_AND_DEVICES.md)。CLI/原生 Mem0 是本机运维入口，
不经过网页 Cookie 权限层；对外 Agent 应通过受控服务端接口接入，不能把运维入口直接暴露给用户。

### 每日记事

每轮对话结束后，会在同一个 `data/dashboard/chat.sqlite` 中写入 `daily_diary_entries`，按北京时间
`YYYY-MM-DD` 归档当前 `(family_id, user_id)` 的跨会话对话。原始问题、回答、状态和本轮新增长期记忆都保留；
`daily_diaries` 保存 LLM 生成的当日摘要与明确的新偏好。摘要在后台整理，失败也不会丢原始对话；
同一天再有新对话会重新整理。旧对话首次启动时回填日记条目，旧日摘要可在页面点「重新整理」生成。
聊天页「查看每日记事」支持选日期、阅读完整记录、重新整理和下载 Markdown。Agent 可用 `mem0`
工具的 `diary` 只读操作读取当前用户的指定日期；这份私人日记不自动写入 Qdrant，也不共享给其他家庭成员。
删除一段对话会同时删除它的日记副本，并使当天摘要失效；已单独写入 Mem0 的长期记忆仍会保留。
日记整理使用当前配置的 LLM，可能产生额外 API 费用；如需关闭后台自动整理，可设置
`MEM0_DIARY_AUTO_SUMMARY=false`，手动整理仍可用。日记摘要不是原始证据，重要事实应回看完整记录。

### 可选 Qwen 重排序

编辑实际启动使用的 `config/qwen.env`（`--profile openai` 则是 `config/openai.env`）：

```dotenv
MEM0_RERANK_ENABLED=false
MEM0_RERANK_MODEL=qwen3-rerank
MEM0_RERANK_URL=https://dashscope.aliyuncs.com/compatible-api/v1/reranks
MEM0_RERANK_API_KEY=
MEM0_RERANK_TIMEOUT=3
MEM0_RERANK_CANDIDATES=30
MEM0_RERANK_TOP_N=8
```

将 `false` 改成 `true` 并重启服务即可开启。默认关闭，不增加模型请求；无需重建向量库。
Qwen 北京预设、上述百炼地址可自动复用 `MEM0_PROVIDER_API_KEY`；其他平台或自定义地址必须显式填写
`MEM0_RERANK_API_KEY`，不会把 OpenAI Key 自动发送给百炼。新业务空间可以把 URL 改为
`https://<WorkspaceId>.cn-beijing.maas.aliyuncs.com/compatible-api/v1/reranks`，Key 必须具有该空间的调用权限。
不要填 `/chat/completions` 或旧的 `text-rerank` 接口；当前适配的是 `qwen3-rerank` 的顶层 `results` 协议。

聊天检索先按用户/家庭/资料范围过滤，扩大各路候选并合并，再把最多 30 条候选发送给重排序 API，
返回前 8 条；这些值由上述配置控制。API 序号必须落在本次候选列表内，结果保留本地 ID、时间和来源。
排序 HTTP 请求在数据库锁之外执行；使用连接池、单阶段 3 秒超时且不重试、不跟随跳转。
超时、限流、鉴权失败或无效响应都会回退到本次候选的原排序，聊天工具卡片显示回退状态。
返回的 `rerank_score` 与原始 `score` 分开保存，不再用旧分数覆盖重排次序。

网页语义搜索、`python -m local_runtime search` 和资料 `search/shell` 同样支持此开关；
它们的输出数量沿用请求的 `limit/top_k`，`MEM0_RERANK_TOP_N` 只控制聊天结果数量。
日记按日期读取、关键词浏览和资料导入不调用排序 API。直接绕过 local_runtime 调用原生 `Memory.search()`
不会自动读取这个开关。长文本会使用受预算限制的前缀参与排序，原文不修改；返回 `input_truncated` 供诊断。
排序只衡量相关性，不自动处理新旧事实冲突，也不保证每次请求都返回足够相关的证据。

```bash
# 仅查看配置，Key 不会显示，也不调用模型。
.venv/bin/python -m local_runtime --env-file config/qwen.env config
# 开启开关并配置 Key 后，用合成文本验证排序 API；会产生模型调用。
.venv/bin/python -m local_runtime --env-file config/qwen.env check --component rerank
```

协议来源：[百炼排序接口](https://help.aliyun.com/zh/model-studio/text-rerank-api)、
[百炼默认北京地址示例](https://help.aliyun.com/en/polardb/polardb-for-postgresql/use-polarsearch-to-build-a-rag-based-solution)。
产品化差距和建议验收条件见 [产品化评估](local_runtime/PRODUCTION_REVIEW.md)。

```bash
bash start.sh --doctor             # 安装/检查环境，不启动服务，不调用云端 API
bash start.sh --port 18581          # 更换端口
bash start.sh --profile openai      # 新的空库可用 OpenAI 预设
```

配置在 `config/`，数据库在 `data/`，原文件及上传在 `materials/`，解析缓存在 `data/materials/`。
可编辑 `config/local.env` 使用自部署 OpenAI 兼容服务，然后 `bash start.sh --profile local`。
第一次使用 local/ollama 前，先复制对应 `.env.example` 并填写地址与模型；默认端点仅为示例。

## 从完整迁移包恢复现有记忆

先停止源服务，并将解压后的迁移包中 `data/`、`materials/`、`config/` 完整复制到本目录对应位置。
目标必须是尚未使用的空数据目录，不能将两份不同数据库合并覆盖。保留原迁移包作为备份。
继续使用 qwen 配置，不更换 embedding 模型、维度或集合。然后执行 `bash start.sh`。
本版在读取原 `/Knowin/foundation/seb/...` 附件路径时映射到当前目录，不需要创建系统软链接。
资料的导入身份使用固定逻辑路径，目录搬家不会重新生成文档身份。不要同时启动两个进程访问同一 data。
复制整个项目到新机器时不要带 `.venv` 和 `.uv-cache`，在新机执行 start.sh 重建环境。

## 格式和依赖边界

- 文本、JSON、CSV、HTML、PDF、图片和视频：Python 依赖由 uv 安装；PyAV 提供音视频解码。
- DOCX：没有 LibreOffice 时可读取正文、表格和图片，不能保证真实页码。
- 老 DOC 格式、Word 精确页码/版面渲染：需要系统安装 LibreOffice，uv 无法安装这项系统程序。
- 图片描述、视频视觉/语音处理的模型能力与 API 仍由既有解析器决定，不代表所有自部署模型都支持。
- spaCy 模型在安装时下载，BM25 模型按需首次下载到 data/models/fastembed。离线使用需预先准备缓存。
- 此代码包不包含私人数据库、原始资料或真实 API Key；首次安装需要联网。原始许可证见 LICENSE。

环境诊断不会调用付费模型。功能测试使用临时数据库及模型替身，不等于已验证云端 Key 或真实回答质量。
