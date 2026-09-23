# 验证记录 — 2026-09-23

- 在本机 macOS arm64 全新创建项目 `.venv`（Python 3.11.15），通过 start.sh 安装 91 个锁定依赖。
- uv pip check 通过；doctor 成功导入 PyAV、PyMuPDF、fastembed、Qdrant、spaCy，并检测到 en_core_web_sm。
- 100 项测试全部通过：runtime / dashboard / materials / videos / chat / grounding / portable。
- 新增验证覆盖旧附件路径映射、真实本地 Qdrant 中的导入去重查询、缓存和文档身份跨目录移动不变、路径越界拦截。
- Ruff 检查与 Bash 语法检查通过。
- 测试使用临时数据及模型替身，未调用付费模型，也未复制或更改任何生产数据库。
- Coder SSH 当前连接超时，因此本次未在 Coder 实机安装或启动，也未确认远端是否比本机代码快照更新。

## 来源

- Mem0 核心：本机保存的 2026-09-21 Docker 构建源码（mem0ai 2.0.20）。
- 网页及聊天：本机保存的 2026-09-22 coder-mem0-support/app/local_runtime。
- 本次新增：start.sh、local_runtime/portable.py、portable_paths.py、便携路径适配和 tests/test_portable.py。
- 本包不包含真实 Key、私人资料或数据库；依赖安装结果留在本地 .venv，不进入发布压缩包。
