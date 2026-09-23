# Docker 封装与迁移

镜像包含 Python 3.11、uv 0.12.6、项目源码、已锁定的 Python 依赖、网页资源，默认包含
LibreOffice Writer 和 Noto CJK 字体。数据库、原始资料和真实 API Key 均通过运行时挂载提供，
不进入镜像或构建上下文。PyAV 包含视频解码库，不依赖宿主机 FFmpeg、Conda 或 GPU。

## 当前服务器的 Docker 兼容性

2026-09-21 检查：服务器 Docker 通过 `/var/run/user/proxy.sock` 提供，缺少 Compose 和 Buildx。
单文件 bind mount 在容器中表现为目录，不能正常读取；整个目录挂载后已验证可读取其中的项目文档。
因此本配置统一使用目录挂载，包括只读配置目录，避免依赖代理不支持的单文件挂载。
切换前仍应核对目标机上的挂载内容，再停止原服务。
当前服务器使用预构建镜像加普通 `docker run` 的方式；镜像在带 BuildKit 的机器上构建后导入。

## 路径约定

容器内部保留已有绝对路径，因此宿主机可以换目录，现有数据库里的路径无需重写。

| 宿主机默认来源 | 容器内目标 | 用途 |
| --- | --- | --- |
| 项目 `.data/` | `/Knowin/foundation/seb/mem0-main/.data/` | Qdrant、历史、任务、解析结果、BM25 缓存 |
| 项目旁的 `material/诺因公开资料/` | `/Knowin/foundation/seb/material/诺因公开资料/` | 原始文件、网页上传文件 |
| `local_runtime/` | `/run/mem0-config/`，只读 | 从选定的 `.env` 文件读取模型预设和 API Key |

不要把整个宿主机项目挂到容器工作目录，否则会覆盖镜像里的代码。宿主机 `.venv` 不参与运行。
配置目录中的 Python 文件不会被执行；应用代码来自镜像。迁移时可准备一个只含模型 `.env` 的配置目录，
设置 `MEM0_CONFIG_PATH` 指向它，并用 `MEM0_PROFILE_NAME` 选择 `qwen.env` 或 `openai.env`。
此方案针对当前已有数据迁移：需要带上 `.data/models/fastembed`，默认离线加载 BM25 缓存。
换 embedding 模型或维度需单独重建向量索引，不能作为普通迁移步骤随意修改。

## 构建、检查与切换

此构建命令需要 Docker BuildKit/Buildx 和 Docker Compose v2 或更新版本；
用 `docker buildx version`、`docker compose version` 验证。Docker Desktop 通常已包含这两项。
只有 `docker` 命令不代表已经安装 Compose。缺少 Compose 时可使用下方纯 Docker 命令，
但两种方式都要求 Docker 正常支持目录 bind mount；当前云环境的兼容性见上文。

在具备上述构建工具的机器上，进入完整项目根目录后执行：

```bash
docker compose -f compose.mem0.yaml config --quiet
docker compose -f compose.mem0.yaml build
```

构建镜像不会打开现有数据库，可以在原网页运行时完成。正式切换时先停止所有本地 CLI/导入进程，
再停止旧网页，确认停止成功后启动容器；同一目录只允许一个 Memory 进程使用。

```bash
bash local_runtime/dashboard.sh stop
# 上一步成功后再执行：
docker compose -f compose.mem0.yaml up -d --no-build
docker compose -f compose.mem0.yaml ps
docker compose -f compose.mem0.yaml logs --tail 80 memory
curl --noproxy '*' -fsS http://127.0.0.1:18580/api/overview
```

宿主机端口只绑定 `127.0.0.1:18580`，延续现有 SSH 转发访问方式；容器内部监听 `0.0.0.0`。
当前页面仍是单人管理工具，用户筛选不是账号鉴权。Docker 不会自动增加家庭权限或 Agent 接口。
容器只有一个 Uvicorn worker；不要扩容多个副本访问同一份本地 Qdrant。

Compose 的 `restart: unless-stopped` 需要 Docker daemon 自身开机运行。手动停止容器后不会自动重启。
停止最长等待 10 分钟，正在导入的任务需要正常退出；未完成任务由现有任务恢复机制处理。

回滚时先 `docker compose -f compose.mem0.yaml down`，确认容器退出，再运行
`bash local_runtime/dashboard.sh start`。镜像与当前源码使用相同依赖锁和数据格式，挂载数据不会因删除容器而删除。

## 服务器没有 Compose 时

以下命令与 Compose 使用同样的三个挂载，适用于支持 bind mount 的 Linux x86_64 宿主机，
不需要安装 Compose/Buildx。先导入在其他机器上构建好的镜像；所有挂载都使用目录，以兼容当前云环境：

```bash
cd /Knowin/foundation/seb/mem0-main
# 如果尚未加载镜像，先执行 docker image load -i /path/to/knowin-mem0-image.tar.gz
docker image inspect knowin-mem0:local --format '{{.Os}}/{{.Architecture}}'
bash local_runtime/dashboard.sh stop
# 确认停止成功后启动；容器名已存在时先检查旧容器，不要重复启动：
docker run -d --name knowin-memory --init --restart unless-stopped --stop-timeout 600 \
  --platform linux/amd64 -p 127.0.0.1:18580:18580 \
  --mount "type=bind,src=$PWD/.data,dst=/Knowin/foundation/seb/mem0-main/.data" \
  --mount "type=bind,src=/Knowin/foundation/seb/material/诺因公开资料,dst=/Knowin/foundation/seb/material/诺因公开资料" \
  --mount "type=bind,src=$PWD/local_runtime,dst=/run/mem0-config,readonly" \
  -e MEM0_DATA_DIR=/Knowin/foundation/seb/mem0-main/.data \
  -e MEM0_DIR=/Knowin/foundation/seb/mem0-main/.data/sdk \
  -e FASTEMBED_CACHE_PATH=/Knowin/foundation/seb/mem0-main/.data/models/fastembed \
  -e MEM0_ENV_FILE=/run/mem0-config/qwen.env \
  knowin-mem0:local
docker logs --tail 80 knowin-memory
curl --noproxy '*' -fsS http://127.0.0.1:18580/api/overview
```

日常启动/停止：`docker start knowin-memory` / `docker stop knowin-memory`。
Compose 与纯 Docker 二选一，不要同时启动它们。纯 Docker 回滚时先停止并删除该容器，再启动旧网页。

## 换一台机器

1. 源机器停止所有记忆写入进程后，备份 `.data/`、完整资料目录及模型配置；保留文件权限。
   SQLite/Qdrant 是文件数据库，不能用边写入边直接复制文件的方式做完整迁移。
2. 导出镜像：`docker image save knowin-mem0:local | gzip > /tmp/knowin-mem0-image.tar.gz`。
   同时携带 `compose.mem0.yaml` 和路径示例。镜像内没有数据和 Key，它们需要单独迁移。
3. 目标机器执行 `docker image load -i /path/to/knowin-mem0-image.tar.gz`。
4. 复制 `local_runtime/docker/paths.env.example` 为自己的路径文件，填写目标机器的数据、资料、配置目录三个路径。
   例如数据位于 `/srv/mem0/data`、资料位于 `/srv/mem0/materials`，均可挂载到固定的容器内路径。
5. 启动：`docker compose --env-file /path/to/paths.env -f /path/to/compose.mem0.yaml up -d --no-build`。
   验证记忆数量、详情历史、原文件预览、视频 Range 播放，再做一次模型检索检查。

默认镜像是 Linux amd64。ARM 机器可尝试 Docker 的 amd64 模拟，或设置 `MEM0_PLATFORM=linux/arm64`
重新构建并验证依赖；不能直接在 Windows/macOS 裸系统执行 Linux 镜像里的程序。
导入预构建镜像后无需重新下载 Python 包，但当前云端模型的检索和导入仍需要联网访问百炼。

## 已完成的验证（2026-09-21）

- 镜像 `knowin-mem0:local` 已构建并导入当前服务器，Linux amd64，约 1.32 GB。
  镜像配置 ID：`sha256:d0d5693a3146c557fdd38d486a6e2aa78a63e675a26942ee8168256f6e9003d2`。
- Python 3.11.16、项目内依赖锁对应的 91 个包通过 `uv pip check`；spaCy 英文模型、PyAV 可加载。
  环境位于镜像的 `/opt/venv`，不依赖宿主机 Conda；镜像不包含真实 `.env` 或 `.data`。
- 服务器容器中 Ruff 和 49 项 runtime/dashboard/materials/videos 测试通过；测试容器关闭网络。
- 独立目录写入两条测试记忆，网页通过测试端口 19581 读取内容、历史和原件。
  删除并重建容器后，记忆 ID、原件字节数和内容校验保持一致。
- LibreOffice 实际将含中文的两页 DOCX 转为两页 PDF；复用固定容器路径后可从网页读取 PDF。
- API Key 使用无效的测试占位值，写入时替换测试 embedding，未调用付费模型 API。
  本次验证容器部署和持久化，不代替正式模型的联网检索验证。
- 原网页仍运行在 18580，原进程及 201 条正式记忆保持不变；尚未把正式服务切换到 Docker。

验证记录和独立测试数据位于 `.data/setup-backups/20260921-docker/`。
本机 ARM Docker 的测试容器曾停留在创建阶段，最终运行验证在服务器的原生 x86_64 Docker 中完成。

## 参考

- [uv 的 Docker 集成](https://docs.astral.sh/uv/guides/integration/docker/)
- [Docker bind mounts](https://docs.docker.com/engine/storage/bind-mounts/)
