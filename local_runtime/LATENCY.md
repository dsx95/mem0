# 记忆检索耗时与优化 · 2026-09-17

本次在服务器 `/Knowin/foundation/seb/mem0-main` 的真实项目 `.venv` 测量现有 Qwen 接口与本地数据库。查询的是 `knowin_public` 资料；没有测试聊天事实写入延迟，没有启动后台常驻服务。

## 结论

主要耗时来自每次独立 CLI 进程重复导入 Python/SDK 模块、初始化 Memory，以及混合检索首次加载 spaCy。当前 182 个 point 的向量检索本身约 11 ms。

首个 embedding 调用另行拆分为 5.657 秒，其中 5.484 秒在首次访问 `client.embeddings` 时加载 `openai.resources` 等模块，实际底层 HTTP 请求为 0.151 秒。因此不能把首次 embedding 的全部时间算作云端模型响应时间。

本轮未修改 embedding 模型、维度、原有混合排序、依赖版本或 API Key。新增复用同一 Memory 和连接的 `materials shell`，解决连续查询反复冷启动的问题。

## 实测数据

未开启 cProfile 的原始 CLI，分别启动独立进程查询“诺因的GLOW架构是什么？”：

| 方式 | 一次完整进程耗时 |
| --- | ---: |
| 原 `local_runtime ... search` 混合检索 | 31.786 秒 |
| 原 `local_runtime.materials search` 语义检索 | 23.855 秒 |

带阶段计时的混合检索定位样本：

| 阶段 | 耗时 |
| --- | ---: |
| 导入 Mem0 | 9.516 秒 |
| 创建 Memory 实例 | 9.711 秒 |
| 首次词形处理 / spaCy 加载 | 5.952 秒 |
| 首次实体抽取 | 0.492 秒 |
| 首次 embedding，包括 SDK 惰性加载 | 5.482 秒 |
| 本地向量搜索 | 0.011 秒 |
| 首次 BM25 编码器加载 | 0.249 秒 |
| 首次查询总耗时，不含之前导入和初始化 | 12.195 秒 |
| 同一进程第二 / 第三次查询 | 0.167 / 0.195 秒 |

阶段定位样本的启动部分开启了 cProfile，不能代替上面的真实 CLI 总时长。BM25 编码器加载包含在关键词检索内，各嵌套阶段不能重复相加。本次没有发现 Hugging Face 下载等待是主要耗时。

新增 shell 分别连续执行 6 条查询：首条为 GLOW 架构，之后查询融资、KnowinBrain、产品特点、感知设备，最后复查 GLOW。每次都实际调用 embedding 并重新查库，没有增加查询结果缓存。

| 模式 | 初始化 | 首条查询 | 后续 5 次查询中位数 | 后续查询范围 |
| --- | ---: | ---: | ---: | ---: |
| 语义检索 | 18.492 秒 | 5.000 秒 | **130.70 ms** | 108.83–705.69 ms |
| 原混合检索 | 15.959 秒 | 11.713 秒 | **260.69 ms** | 131.70–291.56 ms |

两个模式分别与自身原命令的首尾查询结果 ID 完全一致。这是当前数据量和网络条件下的小样本，不能当作并发能力或长期 P95；首次启动成本仍然存在。

## 现在怎样使用

```bash
cd /Knowin/foundation/seb/mem0-main
.venv/bin/python -m local_runtime.materials shell --timings
```

初始化后直接输入问题，一行一个，例如：

```text
诺因的GLOW架构是什么？
诺因融资金额是多少？
Knowin-X1有哪些产品特点？
/exit
```

结果保留文件来源和页码，附 `timing.initialization_ms`、`timing.search_ms`。初始化时间每次输出同一个值用于说明本次会话的启动成本，不表示每次查询重新初始化。

可加 `--hybrid` 使用原混合排序，或使用 `--file` 限定文件。单次命令也支持计时：

```bash
.venv/bin/python -m local_runtime.materials search \
  --query "诺因的GLOW架构是什么？" --timings
```

`shell` 持有本地 Qdrant 文件；先 `/exit` 或 EOF 退出，再另开进程导入。瞬时查询失败只返回脱敏错误并允许下一次查询；401/403 结束会话；退出时释放数据库。

## Agent 集成

将 Memory 创建放在 Agent/服务的生命周期开始处，所有轮次复用；退出时关闭。不要在每一轮用 subprocess 启动 `python -m local_runtime ... search`，也不要每次查询都调用 `create_memory()`。

```python
from local_runtime.runtime import load_settings, create_memory, close_memory
from local_runtime.materials import DEFAULT_ROOT, search_materials

settings = load_settings("/Knowin/foundation/seb/mem0-main/local_runtime/qwen.env")
memory = create_memory(settings)  # 进程启动时只执行一次

try:
    while True:
        query = input("查询（/exit 退出）> ").strip()
        if query == "/exit":
            break
        if not query:
            continue
        result = search_materials(memory, query, DEFAULT_ROOT, "knowin_public", top_k=5)
        print(result)
finally:
    close_memory(memory)
```

上面是当前公共资料入口。普通个人记忆可复用同一对象调用 `memory.search(query, filters={"user_id": trusted_user_id}, top_k=5)`；具体家庭/个人范围应由应用身份确定。

如需多个进程或多设备同时访问，可再提供一个持有 Memory 的服务接口，或将 Qdrant 切换为服务模式；本轮没有新开端口或驻留进程。

## 后续优化优先级

1. 持续复用进程、Memory 和模型 HTTP 客户端。本轮已提供可用的交互入口，Agent 仍需按上例接入自身生命周期。
2. 启动阶段按实际模式预加载所需 SDK/NLP 模块，避免第一位用户承担加载。预热会移动启动成本；发真实预热请求还会产生模型调用，本轮没有默认增加此行为。
3. 已知 ID、家庭成员、房间坐标用结构化查询；这属于尚未实现的 `home_memory.db` 方案，不必为了已知主键再走 embedding。
4. 聊天写入若慢，将普通总结/事实抽取放进可持久化、可重试的队列；明确更正或即时状态仍应先完成业务写入。当前 `add(infer=True)` 会额外调用 LLM，本轮没有对它给出实测秒数，也没有改为后台写入。
5. 重复 query 的 embedding 可考虑按模型/版本/维度/文本做有界缓存；不能直接长期缓存位置或搜索结果。当前约 0.15 秒的 HTTP 样本不足以支持立即换模型或上 GPU 的必要性。

已有 FastEmbed 本地缓存可通过 `HF_HUB_OFFLINE=1` 避免 Hugging Face 更新检查，但应在确认缓存完整的情况下使用；该变量不关闭百炼接口。这是预防网络抖动的选项，本轮未将其设置为全局默认。[官方环境变量说明](https://huggingface.co/docs/huggingface_hub/package_reference/environment_variables#hfhuboffline)

## 验证和回退

- 10 项导入器/交互查询测试通过，包括复用一个实例、文件与用户范围不变、EOF 释放、错误脱敏、瞬时错误后继续、认证失败停止。
- 实际语义/混合模式各 6 条查询成功，首尾结果与原命令一致；单次查询入口回归成功。
- Ruff 与格式检查通过；没有新增依赖。
- 原主集合与 history 仍均为 182 条；原 runtime.py 与三个 env 文件哈希未变。
- 修改前文件备份：`.data/setup-backups/20260917-latency/`。

服务器原始记录位于 `.data/latency/`：`baseline-unprofiled.json`、`profile-hybrid-offline0.json`、`first-embedding-profile.json`、`shell-verification.json`、`shell-semantic-results.json`、`shell-hybrid-results.json`、`oneshot-after.json`。
