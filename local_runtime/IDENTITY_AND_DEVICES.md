# 用户、家庭与设备接口

这是本机无密码模式的接口说明。Cookie 绑定所选用户，但选择用户本身不要求密码。后续上线应替换
`POST /api/identity/select`，由真实登录结果签发身份 Cookie；不应信任 Agent 或客户端随意提交的 user_id。

## 请求身份

先通过 `GET /api/identity/users` 查询已存在的本地用户，再调用
`POST /api/identity/select`，JSON 为 `{"user_id":"alice"}`，保存返回的 Cookie。
写请求必须有 `X-Memory-Client: dashboard`；普通 JSON 请求使用 `Content-Type: application/json`。
页面每次数据请求额外带 `X-Memory-User: alice`，防止另一个标签页切换身份后误读/误写。服务器从 Cookie 取身份，
此 Header 只做一致性检查，不能用来冒充用户。不要把 Cookie 写入公开日志。

| 接口 | 用途 / 参数 |
|---|---|
| `GET /api/identity/me` | 当前用户、所属家庭/成员、可用设备 |
| `POST /api/identity/users` | 创建本地用户：`user_id, name` |
| `POST /api/identity/families` | 当前用户创建家庭：`family_id, name` |
| `POST /api/identity/families/{family_id}/members` | 家庭创建者添加已有用户：`user_id` |
| `POST /api/identity/devices` | 登记设备：`device_id, name, family_id`；family_id 空表示个人设备 |
| `GET /api/manage/memories` | `memory_type=builtin/longterm/all, family_id, device_id, q, page, page_size, include_hidden, fact_status=all/active/disputed/retracted` |
| `POST /api/manage/notes` | 手动添加：`text, family_id, device_id, scope=personal/family, memory_type=builtin/longterm；长期事实增加 subject, attribute, occurred_at` |
| `POST /api/uploads` | 原始文件请求体；查询参数 `filename, family_id, device_id, scope`；`Content-Type: application/octet-stream` |
| `GET /api/manage/conversations` | 当前用户对话：`family_id, device_id, q, page`；q 搜索对话标题 |
| `GET /api/manage/diaries` | 当前用户按天归档：`family_id, device_id, page` |
| `GET /api/memories/{id}` | 授权范围内的详情与修改历史 |
| `GET /api/memories/{id}/file?download=true` | 授权范围内的源文件 |
| `POST /api/memories/{id}/visibility` | `{"hidden":true}` 隐藏，false 恢复；只影响当前用户 |
| `DELETE /api/memories/{id}` | 删除授权记录及其历史和生成副本 |
| `DELETE /api/chat/sessions/{id}` | 删除自己的整段对话及相应日记副本 |
| `DELETE /api/chat/sessions/{id}/turns/{turn_id}` | 删除自己的一轮对话及相应日记副本 |
| `DELETE /api/chat/sessions/{id}/diary?date=YYYY-MM-DD` | 删除该会话所属用户/家庭当天的日记和原始轮次；跨会话生效 |

列表筛选：省略/空值表示全部可见范围，`__none__` 表示个人空间或未标记设备。新增记录/聊天则使用空字符串
表示无家庭/无设备，不能传 `__none__`。ID 支持 1–64 位字母、数字、点、下划线、短横线，首字符为字母或数字。
设备 ID 全局唯一，必须与当前家庭或个人空间匹配。

创建聊天示例：

```json
{
  "user_id": "alice",
  "family_id": "home",
  "device_id": "robot_living_room",
  "use_library": true
}
```

发送到 `POST /api/chat/sessions`。user_id 必须等于 Cookie 用户，家庭和设备必须在服务端关系表中。
后续 `POST /api/chat/sessions/{id}/messages` 使用 `text` 和唯一 `request_id`，可附带同样的
`user_id, family_id, device_id, remember_scope`；携带的身份必须与会话一致。切换设备应新建/切换会话。

## 读取、删除边界

- 当前家庭的个人长期记忆跨设备沿用。内置资料检索结合用户/家庭授权和设备筛选。
- 短期上下文是当前会话最近 12 轮完成对话，完整历史与日记始终只属于本人。
- 日记按 `(user_id, family_id, 北京日期)` 跨设备汇总。日记详情/删除可附加 `device_id` 精确筛选；
  这里省略参数表示所有设备，显式 `device_id=` 表示未标记设备。设备筛选时只展示对应原始条目，不展示全设备摘要。
- 私人记录仅本人可删除；共享记录仅创建者或家庭创建者可删除；公共资料只能对自己隐藏。
- 删除日记会同步删原始轮次，避免重启回填；删除对话不会连带删除独立长期事实。
- 删除长期记忆会删除向量、修改历史、工具追踪与日记新增记忆副本，失效相关摘要；原始问答、资料源文件、解析缓存和备份保留。
- 删除与活跃回复冲突时返回 409，等待/停止回复后重试；正在导入同份资料时不能删除其分片。
- 隐藏/删除的记录不会进入新的 LLM/重排候选；已经写入原始对话的文本不因隐藏记录自动消失。

部署仍为单进程 SQLite + Qdrant Local。关系表、Cookie 哈希、隐藏状态和删除重试标记与聊天共用
`data/dashboard/chat.sqlite`；向量和修改历史沿用原来的数据库，不因用户或设备数量新建数据库。
CLI 和原生 Mem0 不经过这层网页授权，只能作为受信任的运维接口。

长期事实的版本、冲突确认、撤回及同步任务接口见 [事实管理说明](FACTS.md)。
`GET /api/manage/records/{id}` 可查看有权限的隐藏记录和历史；正常召回仍过滤隐藏记录。
删除长期事实同时清理全部版本和候选；Qdrant 清理失败会持久重试，删除标记即时阻止读取。

待办清单使用同一身份与家庭/设备权限入口，详见 [待办接口](TODOS.md)。共享事项负责人只能更新状态和子任务，创建者或家庭创建者可管理和删除。
