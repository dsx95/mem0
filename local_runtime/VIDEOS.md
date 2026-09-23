# 视频资料导入 Mem0

入口与其他资料相同，默认使用 `local_runtime/qwen.env` 中已有的百炼 API Key。
视频解码依赖 PyAV，安装在本项目 `.venv` 中，不需要安装系统 FFmpeg。

```bash
cd /Knowin/foundation/seb/mem0-main

# 逐个导入；可重复传 --file 选择多份。
.venv/bin/python -m local_runtime.materials ingest --file "knowindream.mp4"

# 只检索某一段视频。
.venv/bin/python -m local_runtime.materials search \
  --file "原型机/原型机2026-3-4 20s.mp4" \
  --query "视频里机器人有哪些可见动作？" --top-k 5

# 连续查询，保持初始化好的 Memory 实例。
.venv/bin/python -m local_runtime.materials shell --timings
```

## 处理和存储

1. 校验视频文件，解码完整画面与第一条音轨；无法完整解码的文件不会进入模型调用和写入步骤。
2. 默认每秒采样 1 帧，每 10 秒一组，使用 `qwen-vl-plus` 生成带时间范围的中文画面记录。
3. 音轨转为 16 kHz 单声道 WAV，以 30 秒窗口、1 秒重叠调用 `qwen3-asr-flash`。数字静音、空转写或只有音乐符号的结果不生成音轨记忆。
4. 画面记录与音轨转写分别分块，以 `infer=False` 写入 Mem0，使用原有 `text-embedding-v4` 生成 1024 维向量。

视频原文件继续保留在资料目录。Qdrant 中保存的是提取的文字、向量以及来源与时间信息。
当前资料范围仍为 `user_id=knowin_public`、集合 `mem0_7a008ae9dff3`。

| 字段 | 含义 |
| --- | --- |
| `source_file` / `source_path` | 视频相对路径 / 原始绝对路径 |
| `source_sha256` | 本次导入的文件内容版本 |
| `extraction_method` | `video_sampled_frames` 或 `video_asr` |
| `start_seconds` / `end_seconds` | 相对于视频起点的片段范围，单位秒 |
| `video_duration_seconds` | 视频总时长 |
| `sampled_frame_times` / `preview_files` | 实际采样帧的时间与缓存路径，仅画面记录 |
| `audio_file` / `timestamp_precision` | 音频窗口与时间精度说明，仅转写记录 |

命中一条 `start_seconds=20`、`end_seconds=30` 的记录时，Agent 可引用“原视频 00:20–00:30”，
再用原视频核对。普通聊天模型不会直接收到视频二进制。

## 参数与边界

- `--video-segment-seconds 10`：画面分段长度，支持 2–30 秒。
- `--video-sample-fps 1`：每秒采样帧数，支持 0.2–4；每段最多 32 张采样帧。
- `--video-workers 3`：视觉与转写请求并发数，支持 1–4。
- `--vision-model qwen-vl-plus` / `--asr-model qwen3-asr-flash`：可替换为接口兼容且已开通的模型。

当前单视频限制 30 分钟。每秒采样可能漏掉瞬间动作，不能据此断言连续操作成功；
画面说明和音轨转写都是模型输出，精确引用应回看视频。音频时间是窗口范围，未做逐字时间对齐。
只处理第一条视频轨、第一条音轨；配乐中的歌词也可能被转写，不应自动视作讲解或产品事实。
重叠音频窗口可能产生重复文字，检索结果尚未增加跨片段语义去重。

完整文件解析、单段模型响应和已入库片段均有缓存；同文件同参数重复导入可跳过已完成工作。
更换文件、采样参数或模型会形成新记录版本；当前不自动删除旧版本。

`X1.mp4` 在 2026-09-18 的检查中无法解码：缺少 MP4 索引，且文件长度小于内部数据块声明长度。
重新传入完整文件后，运行以下命令重试：

```bash
.venv/bin/python -m local_runtime.materials ingest --file "X1.mp4"
```

模型接口说明：[百炼视觉理解](https://help.aliyun.com/zh/model-studio/vision)、
[Qwen ASR 接口](https://help.aliyun.com/zh/model-studio/qwen-asr-api-reference)。
