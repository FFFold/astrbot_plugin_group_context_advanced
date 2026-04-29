# 更新日志

## v0.2.0

### 新增功能
- 群聊白名单/黑名单配置，支持按群号或 `unified_msg_origin` 限制插件生效范围，黑名单优先级高于白名单
- `session_chat_maxlen` 配置项，可调整每个群聊的未处理消息缓存上限（默认 500，-1 不限制）
- `forward_max_messages` 配置项，限制合并转发消息的最大解析条数，防止 token 超限

### 重大变更
- 移除主动回复功能，插件回归专注于增强的群聊上下文管理
- 移除私聊场景会话控制，简化插件职责
- `conversation_rounds_limit` 替换为 `max_context_rounds` + `dequeue_context_rounds`，模仿框架的集中丢弃算法，一次性丢弃多轮以提升缓存命中率
- System prompt 注入改用 `[GCPLUGIN]` 标记精确清洗，避免残留

### 兼容性修复
- 修复与 `astrbot_plugin_livingmemory` 等插件的 prompt 冲突，不再覆盖而是追加后按标记剔除
- 同时提供多媒体 content 和纯文本 prompt，保证与其他插件的兼容性

### 安全加固
- 移除 `file://` 和裸本地路径读取分支，防御任意文件读取漏洞
- `_encode_image_bs64` 中 HTTP 下载的临时文件使用后自动清理，防止磁盘泄漏

### 稳定性改进
- `session_chats` 改用 `deque(maxlen=500)` 防止消息缓存无限制增长
- LLM 请求失败时保留群聊上下文，`session_chats` 延迟到 `on_llm_response` 成功后才清空已消费消息
- 仅清理本次请求实际消费的消息条数，避免 LLM 生成期间新群消息被误删
- 清空后自动回收闲置的 session key，减少长期运行的内存残留
- `BaseException` 改为 `Exception`，避免吞掉 `KeyboardInterrupt` 等系统级异常
- `sender.nickname` 使用 `getattr` 防御性访问，避免平台适配器返回 None 时崩溃
- `isinstance` 使用元组简写，`import base64` 移至顶部导入
- 删除未被调用的 `_extract_forward_content` 死代码
- 空 `chat_text` 不再拼接无用的 Marker
- `_conf_schema.json` 全部字段拆分 `description` 和 `hint`，提升 WebUI 可读性

## v0.1.0

### 初始重构版本
- 基于 AstrBot 内置群聊上下文进行优化和增强
- user/agent 对形式的上下文，每次请求仅包含上一轮请求后新增的群聊消息
- 支持 QQ 合并转发消息分析（仅 aiocqhttp 平台）
- 支持原生 URL 图片自动嵌入或转述描述两种模式
- 支持自定义 system 提示词
- 支持指令消息过滤
