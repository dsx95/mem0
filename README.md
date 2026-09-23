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
