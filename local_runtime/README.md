# 项目内 uv 环境与可切换模型接口

项目：`/Knowin/foundation/seb/mem0-main`。环境：该目录下 `.venv`，uv 缓存在 `.uv-cache`。

安装的是当前目录的 Mem0 源码（editable，`mem0ai 2.0.20`）。`local_runtime/` 把已有模型适配器的参数暴露为配置、Python 函数和 CLI；不需要运行原仓库 Docker 栈。

支持两种协议：

| `PROVIDER` | 用途 | `BASE_URL` 示例 |
|---|---|---|
| `openai` | 云端 API、vLLM/SGLang 等自部署 OpenAI 兼容服务 | `https://供应商地址/v1`、`http://模型服务器:8000/v1` |
| `ollama` | 已运行的原生 Ollama 服务 | `http://模型服务器:11434` |

LLM 和 Embedding 的协议、地址、模型、密钥各自独立，可以混用。这里没有部署模型权重、推理服务器或新的 HTTP 网关；Python 接口供你的 Agent 直接调用。原生 Anthropic/Gemini 等协议不在这次薄配置层内，若使用这些厂商的兼容接口则按 `openai` 配置。

## 1. 使用环境

```bash
cd /Knowin/foundation/seb/mem0-main
source .venv/bin/activate
python -m local_runtime config
```

也可以始终使用绝对解释器 `.venv/bin/python`，避免 Shell 内其他环境影响。`config` 只读取并脱敏展示配置，不发出模型请求；默认 `.env` 留空 LLM 模型名和真实密钥，会显示 `configured: false`，直到你补齐配置。

重建/同步同样的环境：

```bash
cd /Knowin/foundation/seb/mem0-main
bash local_runtime/setup_uv.sh
```

该脚本用 `uv venv` 和 `uv pip sync` 安装 `local_runtime/requirements.lock`。锁文件面向本次 Linux / Python 3.11 环境；核心 SDK 通过 `-e .` 指向本地源码。它不会覆盖已存在的 `.env`。本工作流不是根目录的 `uv sync`，也没有把原 Poetry 锁文件改成 uv 项目锁。

需要主动升级依赖时再执行：

```bash
UV_CACHE_DIR="$PWD/.uv-cache" uv pip compile --upgrade \
  --python .venv/bin/python local_runtime/requirements.in \
  -o local_runtime/requirements.lock
bash local_runtime/setup_uv.sh
```

## 2. 两份只填一次 API Key 的云端配置

服务器已提供以下文件。每份只需填写一行 `MEM0_PROVIDER_API_KEY=`，同一密钥供两个模型使用；`MEM0_PRESET` 是固定标识，无需修改。

| 配置文件 | LLM | Embedding | 维度 | API Key |
|---|---|---|---|---|
| `/Knowin/foundation/seb/mem0-main/local_runtime/openai.env` | `gpt-4.1-mini` | `text-embedding-3-small` | 1536 | OpenAI 平台的 Key |
| `/Knowin/foundation/seb/mem0-main/local_runtime/qwen.env` | `qwen-plus` | `text-embedding-v4` | 1024 | 百炼北京地域的 Key |

两套模型都走各自平台。同平台的 Key 需有这两个模型的调用权限。Qwen 使用仍受支持的北京公共域名 `https://dashscope.aliyuncs.com/compatible-mode/v1`，因此无需填写业务空间 ID。

例如，编辑 Qwen 文件后查看配置：

```bash
cd /Knowin/foundation/seb/mem0-main
nano local_runtime/qwen.env
.venv/bin/python -m local_runtime --env-file local_runtime/qwen.env config
```

`config` 不发请求，输出会隐藏密钥。填写后测试两个真实接口（会产生模型调用）：

```bash
.venv/bin/python -m local_runtime --env-file local_runtime/qwen.env check
.venv/bin/python -m local_runtime --env-file local_runtime/openai.env check
```

Agent 中通过同一个参数选择配置：

```python
from local_runtime import create_memory, load_settings

settings = load_settings("/Knowin/foundation/seb/mem0-main/local_runtime/qwen.env")
memory = create_memory(settings)
```

现有根目录 `.env` 保持原样；运行时用 `--env-file` 或 `load_settings()` 明确选择。两个配置文件均以权限 `600` 创建并被 Git 忽略。可从 `local_runtime/env/openai.env.example` 与 `qwen.env.example` 重新复制空白模板。

固定预设定义在 `local_runtime/presets.py`。启用预设时，平台、模型、维度和两个组件的密钥由预设与 `MEM0_PROVIDER_API_KEY` 决定，旧的 `MEM0_LLM_*` / `MEM0_EMBEDDING_*` 环境变量不会覆盖它们。进程中的 `MEM0_PROVIDER_API_KEY` 会覆盖文件中的 Key；切换文件时也应留意这一项。未启用预设的高级配置仍沿用原来的规则。

Qwen 预设会明确发送 `enable_thinking=false` 供 JSON 记忆抽取使用，并把 `text-embedding-v4` 请求拆成每批最多 10 条。两份预设都明确发送 embedding 维度。更换预设会自动使用不同的向量集合；旧数据如需沿用，需重新生成向量。

预设依据（2026-09-15 核对）：[OpenAI GPT-4.1 Mini](https://developers.openai.com/api/docs/models/gpt-4.1-mini)、[OpenAI embeddings](https://developers.openai.com/api/docs/guides/embeddings)、[百炼兼容接口与地域](https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope)、[百炼结构化输出](https://www.alibabacloud.com/help/zh/model-studio/qwen-structured-output)、[百炼 embedding](https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api)。本次仅用本地模拟接口验证调用链；真实云端调用需填入有效 Key 后执行 `check`。

### 高级配置：独立指定接口与模型

编辑项目根目录 `.env`，设置以下参数：

```dotenv
MEM0_LLM_PROVIDER=openai
MEM0_LLM_BASE_URL=https://你的服务地址/v1
MEM0_LLM_MODEL=实际聊天模型名称或服务ID
MEM0_LLM_API_KEY=实际密钥

MEM0_EMBEDDING_PROVIDER=openai
MEM0_EMBEDDING_BASE_URL=https://你的向量服务地址/v1
MEM0_EMBEDDING_MODEL=实际向量模型名称或服务ID
MEM0_EMBEDDING_API_KEY=实际密钥
MEM0_EMBEDDING_DIMS=1536
MEM0_EMBEDDING_SEND_DIMENSIONS=false
```

厂商地址可能包含 `/api/v3` 等路径，按厂商的 OpenAI 兼容 base URL 填写；不是一律追加 `/v1`。不要填完整的 `/chat/completions` 或 `/embeddings` 地址。

LLM 需要支持 Chat Completions 和 Mem0 使用的 JSON 输出方式；返回 JSON 并不等于保证符合记忆抽取 schema，模型效果仍需真实验证。相关协议：[OpenAI JSON 输出说明](https://developers.openai.com/api/docs/guides/structured-outputs)。

`MEM0_EMBEDDING_DIMS` 是实际向量长度。`SEND_DIMENSIONS=false` 表示只把维度告诉本地数据库，不向模型 API 发送可选的 `dimensions` 参数，适配固定维度的自部署服务。只有服务支持向量降维参数且确实需要时才设为 `true`。

`.env` 通过 python-dotenv 作为数据读取，不需要 `source .env`。进程里的同名 `MEM0_*` 环境变量优先于文件值；可以只通过进程环境注入密钥。文件不做 `${...}` 插值，防止无意引用到另一套凭据。真实密钥不要写进示例文件。

## 3. 配置自部署服务

保留当前云端配置，另建独立配置文件：

```bash
cd /Knowin/foundation/seb/mem0-main
(umask 077; cp local_runtime/env/local.env.example local_runtime/local.env)
# 编辑 local_runtime/local.env，替换 CHANGE_ME、端口和实际 embedding 维度
python -m local_runtime --env-file local_runtime/local.env config
```

例如 LLM 位于 `http://192.168.x.x:8000/v1`，Embedding 位于 `http://192.168.x.x:8001/v1`，把两个地址分别写到配置里。`127.0.0.1` 指运行 Mem0 的这台服务器，不是你的电脑或另一台模型服务器。

没有启用鉴权的自部署 OpenAI 兼容服务可以使用占位 Key `local-unused`；启用鉴权时必须填写真实服务 Key。模型名必须与服务公开的名称一致。示例没有假定任何模型已经部署。

原生 Ollama 使用另一个模板：

```bash
(umask 077; cp local_runtime/env/ollama.env.example local_runtime/ollama.env)
# 填写已安装的模型名与实际 embedding 维度
python -m local_runtime --env-file local_runtime/ollama.env check
```

适配层先检查 `/api/tags` 中是否存在指定模型，缺模型时报告错误，不主动下载权重。原生 Ollama 示例按无鉴权本地/内网服务配置；若前面有需要 Bearer Key 的代理，可使用它支持的 OpenAI 兼容接口。

## 4. 接口与连通验证

```bash
# 仅查看配置，零模型请求
python -m local_runtime config

# 以下命令会向你配置的服务发出实际模型请求
python -m local_runtime check --component llm
python -m local_runtime check --component embedding
python -m local_runtime check

# 独立调用 LLM：不需要 Embedding 配置，也不开记忆数据库
python -m local_runtime llm '请用一句话介绍你自己'

# 写入并检索用户记忆
python -m local_runtime add '我不吃香菜' --user-id user_001
python -m local_runtime search '我的饮食偏好是什么' --user-id user_001 --top-k 5

# 已由上层确认的事实可跳过 LLM 抽取；仍会请求 Embedding
python -m local_runtime add '用户不吃香菜' --user-id user_001 --raw
```

`check` 验证 LLM JSON 返回及 Embedding 批量输出的数量、维度。它不写入记忆。默认请求超时 30 秒，OpenAI 兼容客户端默认最多重试 1 次；原生 Ollama 只设置超时。没有配置备用云端地址，服务失败时不会自动切换其他厂商。

## 5. 在 Agent 里调用

从项目根目录运行你的程序，或把项目路径加入你的程序导入路径：

```python
from local_runtime import create_llm, load_settings

settings = load_settings()  # 默认项目/.env
# settings = load_settings("local_runtime/local.env")
llm = create_llm(settings)
answer = llm.generate_response(messages=[{"role": "user", "content": "你好"}])
print(answer)
```

记忆接入使用同一份配置：

```python
from local_runtime import close_memory, create_memory, load_settings

settings = load_settings("local_runtime/local.env")
memory = create_memory(settings)
try:
    # 回答前，把 results 作为参考资料传给原有 Agent。
    result = memory.search("我的饮食偏好", filters={"user_id": "user_001"}, top_k=5)
    facts = [item["memory"] for item in result["results"]]

    # 对话完成后写新增内容；实时语音服务建议放入有序后台任务。
    memory.add([{"role": "user", "content": "我不吃香菜"}], user_id="user_001")
finally:
    close_memory(memory)
```

实际 Agent 不必使用 `create_llm()` 回答用户；可以继续使用原来的 LLM。`create_memory()` 中的 LLM 用于记忆抽取，二者职责独立。复用一个 Memory 实例，退出时关闭，避免多个进程争用同一个本地 Qdrant 目录。

## 6. 存储与可选依赖

- 默认向量库位于项目 `.data/qdrant`，历史库位于 `.data/<collection>_history.db`。
- 默认 collection 根据 Embedding 协议、地址、模型、维度派生。切换向量模型会使用不同 collection，不会混合不兼容的向量；旧记忆仍在原 collection。只换 API Key 不会换 collection。
- 如果新地址实际上指向同一个向量模型，想继续使用旧 collection，可以明确设置 `MEM0_COLLECTION`，并自行保证模型、维度兼容。换模型后复用旧 collection 需要先做数据迁移/重建向量。
- 默认关闭 Mem0 telemetry，并在首次导入 SDK 前将其辅助目录设置为项目 `.data/sdk`；已设置的进程级 `MEM0_DIR/MEM0_TELEMETRY` 会被保留。建议先通过本模块初始化再导入其他 Mem0 模块。
- 本次安装基本语义记忆链路及 OpenAI/Ollama 客户端，没有安装 Torch、spaCy、fastembed 或下载模型。缺少可选依赖时，Mem0 会提示 BM25/实体功能不可用；语义检索仍可运行。
- 原 SDK 中环境变量 `OPENROUTER_API_KEY` 会覆盖 OpenAI provider 的目标地址。本入口检测到它时会报错；若要使用 OpenRouter，请在当前进程中去掉该变量，然后使用本配置里的独立地址和 Key。

## 7. 免费本地验证

```bash
cd /Knowin/foundation/seb/mem0-main
.venv/bin/ruff check local_runtime
.venv/bin/ruff format --check local_runtime
.venv/bin/python -m pytest local_runtime/test_runtime.py -q
uv pip check --python .venv/bin/python
```

测试用本地 HTTP 服务模拟模型，使用真实 Mem0 provider、真实 Qdrant 和 SQLite，覆盖独立地址/密钥、原生 Ollama、混合部署、维度错误、API 故障、脱敏配置、记忆写入、关闭重开和用户隔离。模拟测试不代表某个真实模型的接口、抽取质量或延迟已经验证。

## 8. spaCy 与 BM25 依赖

从 2026-09-16 起，`local_runtime/requirements.in` 和锁文件包含：

- 本地 Mem0 的 `nlp` 扩展与兼容的 spaCy 3.8 系列。
- 官方模型包 `en_core_web_sm==3.8.0`，供词形还原和完整 NLP 流程共同使用。这两个提示对应同一个模型包。
- CPU 版 `fastembed`，供 Qdrant 的 `Qdrant/bm25` 稀疏关键词编码使用。

依赖和 spaCy 模型均由 uv 安装在项目 `.venv`，以后仍使用 `bash local_runtime/setup_uv.sh` 同步。模型 wheel 的官方 URL 已写入依赖文件，避免依赖 spaCy 启动时自动调用 pip 下载模型。

通过 `local_runtime` 使用时，BM25 辅助资源默认缓存在项目 `.data/models/fastembed`，避免依赖系统临时目录；已有 `FASTEMBED_CACHE_PATH` 环境变量会优先生效。

新增记忆会在写入时生成 BM25 稀疏向量。安装依赖不会自动为已有记忆补齐 BM25 向量或实体索引；旧记忆仍可通过语义向量检索，若需要参与完整混合检索，应另外进行回填。

当前 Mem0 源码固定加载英文 `en_core_web_sm`，安装这个依赖并不等于获得中文分词、中文实体识别或跨语言关键词匹配；中英文语义匹配仍由配置的 embedding 模型承担。

来源：[spaCy 模型安装说明](https://spacy.io/usage/models)、[官方 en_core_web_sm 3.8.0](https://github.com/explosion/spacy-models/releases/tag/en_core_web_sm-3.8.0)、[FastEmbed 文档](https://qdrant.tech/documentation/fastembed/fastembed-quickstart/)。

## 公开资料导入

已增加独立入口 `python -m local_runtime.materials`，支持逐文件导入 PDF、固定分页的 Word、
HTML、TXT、PNG/JPG 和 GIF，并保留来源、页码、图片局部或帧时间。
完整命令、缓存说明和 Word 分页约定见 [MATERIALS.md](MATERIALS.md)。
