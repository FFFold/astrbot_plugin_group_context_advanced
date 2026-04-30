# 群聊上下文感知增强插件

> 修改优化自https://github.com/zz6zz666/astrbot_plugin_group_context

基于 AstrBot 内置的群聊上下文进行优化和增强，提供以下核心功能：
 - 📝 **群聊记录追踪**: 自动记录群聊消息，为 AI 提供上下文信息
 - 📷 **合并转发消息分析**: 支持直接发送或回复引用合并转发消息
 - 🖼️ **图片识别**: 支持原生 URL 图片自动嵌入或转述描述两种模式
 - 🔄 **对话轮数管理**: 模仿框架的集中丢弃策略，最大化缓存命中率
 - ⚙️ **灵活配置**: 支持自定义提示词、指令过滤等配置

## 插件使用
- 使用本插件时，请务必关闭 AstrBot 内置的 `群聊上下文感知` 功能

## 核心特性

- **优化的上下文管理**：采用 user/assistant 对形式的上下文，仅注入上一轮请求后新增的群聊消息，提升群聊体验
- **缓存友好的 system 提示词**：支持自定义 system 提示词，注入在新增群聊历史之后、当前触发消息之前，并通过 `[GCPLUGIN]` 标记精确清洗残留，避免重复注入
- **合并转发消息分析**：支持直接发送或回复引用合并转发消息，自动分析内容并记录到上下文
- **图片识别支持**：除模型图像转述外，还支持原生 URL 图片自动嵌入，可配置两种嵌入方式
- **图片携带轮数控制**：支持配置只保留最后 N 个用户消息中的图片，将前面的图片转换为 `[图片]` 占位符，用于节省请求 token
- **集中丢弃算法**：额外提供 `max_context_rounds` 和 `dequeue_context_rounds` 设置，模仿 AstrBot 框架的 `truncate_by_turns` 策略 —— 对话轮数超出上限时，一次性丢弃 `dequeue_context_rounds` 轮，而非逐轮丢弃，大幅提升 LLM 缓存命中率
- **双格式内容兼容**：同时提供多媒体 content 和纯文本 prompt，保证与其他插件的兼容性

## 配置说明

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `session_chat_maxlen` | int | `500` | 每个群聊最多缓存的未处理消息条数，`-1` 表示不限制 |
| `group_whitelist` | list | `[]` | 启用插件的群聊白名单，填写群号或 `unified_msg_origin`，留空表示不限制 |
| `group_blacklist` | list | `[]` | 禁用插件的群聊黑名单，填写群号或 `unified_msg_origin`，黑名单优先级高于白名单 |
| `max_context_rounds` | int | `-1` | 最多携带的 user/assistant 对话轮数，`-1` 表示不限制 |
| `dequeue_context_rounds` | int | `2` | 超出上限时一次丢弃的对话轮数 |
| `system_prompt` | string | — | 注入当前请求的 system 提示词 |
| `enable_image_recognition` | bool | `false` | 是否启用群聊图片识别 |
| `image_carry_rounds` | int | `2` | 图片携带轮数，更早的图片转为 `[图片]` 占位符 |
| `image_caption` | bool | `false` | 是否启用图片描述（转述）模式 |
| `image_caption_prompt` | string | — | 图片描述提示词 |
| `image_caption_provider_id` | string | `""` | 图片描述的 Provider ID（留空使用当前 Provider） |
| `enable_forward_analysis` | bool | `true` | 是否启用合并转发消息分析（仅 aiocqhttp） |
| `enable_command_filter` | bool | `true` | 是否启用指令消息过滤 |
| `command_prefixes` | list | `["/"]` | 指令消息前缀列表 |

## 与内置插件的区别

本插件主要改进包括：

1. AstrBot 框架原有的群聊上下文感知功能会将内容插入到系统提示词 `system_prompt` 的结尾，容易破坏前缀缓存。本插件复用原生的 **user/assistant 对**形式，仅把上一轮请求后新增的群聊消息注入到对话历史中。

2. **缓存友好的 system 提示词**：支持自定义提示词内容，并以 `[GCPLUGIN]` 标记注入在“新增群聊历史”和“当前触发消息”之间。下一轮请求会清理旧的 `[GCPLUGIN]` 提示词，但由于标记位于新增群聊历史之后，大段历史内容仍能尽量保持稳定前缀，减少缓存失效。

3. **当前触发消息去重**：当缓存消息的 `message_id` 与当前触发事件精确匹配时，插件不会把当前触发消息重复注入到群聊历史中，避免它同时出现在插件上下文和 AstrBot 当前 prompt 里；如果无法精确匹配，则保守保留缓存内容，不猜测裁剪最后一条消息。

4. **合并转发消息分析**，支持直接发送或回复引用合并转发消息，自动分析内容并记录到上下文。

```json
[
  {
    "role": "user",
    "content": "上一轮对话中的用户消息"
  },
  {
    "role": "assistant",
    "content": "上一轮助手回复"
  },
  {
    "role": "user",
    "content": [
      {
        "type": "text",
        "text": "[群友A/10:11:08]: test1"
      },
      {
        "type": "text",
        "text": "[群友B/10:11:11]: test2"
      }
    ]
  },
  {
    "role": "system",
    "content": "[GCPLUGIN]\nYou are now in a chatroom. The chat history is as above. Now, new messages are coming."
  },
  {
    "role": "user",
    "content": [
      {
        "type": "text",
        "text": "当前触发 LLM 的消息<system_reminder>...</system_reminder>"
      }
    ]
  },
  {
    "role": "assistant",
    "content": "本轮助手回复"
  }
]
```
5. **图片识别**，除了支持 AstrBot 原始上下文感知中的模型图像转述方式，还支持对群聊中的原生 url 图片进行自动嵌入；包括合并转发中的图片也支持根据配置项使用这两种嵌入方式。当未开启时，所有图片以 `[图片]` 占位符替代。

<img width="1246" height="890" alt="image-1" src="https://github.com/user-attachments/assets/a022f52f-ac2c-484b-8600-358331744a28" />

6. **多媒体 content 和纯文本 prompt** 同时具备。除了上述 JSON 中展示的一样，插件重新构造了一个 content 字段为列表格式的 user 字段，我们还提供了利用 prompt 字段中的纯文本提示词支持。这样做保证了如果当其他插件用到 on_llm_request 钩子以及 prompt 进行修饰时，prompt 中可以提供 text 格式的请求内容，本质上是为了兼容其他插件。其中 prompt 中的图片 url 将会被替换为 `[图片]` 占位符。插件另配备了一个优先级极低的钩子，用于将 prompt 置为空，保证了实际 llm 请求的时候不会出现重复的请求内容。

<img width="1259" height="363" alt="image-2" src="https://github.com/user-attachments/assets/ca52cd60-b219-42fc-b16a-19cc3812c46c" />

> 上面是控制台看到的额外构建的 prompt 字段，采用 \n---\n 作为群聊消息的分隔符，并且图片 url 也被替换为 [图片] 占位符。但是经过后一个钩子，最终请求的时候这个额外的 prompt 字段会被置空。

## 注意事项

- 请确保禁用 AstrBot 内置的群聊上下文感知功能，避免冲突
- 图片描述功能需要配置支持多模态的 Provider
- `max_context_rounds` 设为 `-1`（默认）时不做限制，完全依赖框架自身的截断策略

## 开发者

- 作者: Fold
- 版本: v0.2.2
- 仓库: https://github.com/FFFold/astrbot_plugin_group_context

## 许可证

本插件基于 GPLv3 许可证发布。
