# AstrBot DSH 桥接插件（astrbot_plugin_dsh_bridge）

[![Version](https://img.shields.io/badge/version-v1.1.0-blue.svg)](https://github.com/yvdi-abc/astrbot_plugin_dsh_bridge)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![AstrBot](https://img.shields.io/badge/AstrBot-Plugin-orange.svg)](https://github.com/Soulter/AstrBot)

将 **DSH（DeepSeek Harness）** 与 **QQ** 双向桥接：QQ 群/私聊的消息自动转发到 DSH 会话，DSH 的新回复自动转发回 QQ。

## ✨ 功能特性

### 🌉 双向桥接

- **QQ → DSH**：白名单内群/用户发的消息转发到绑定的 DSH 会话（触发方式可配置）
- **DSH → QQ**：轮询 DSH 会话，新回复自动转发回 QQ（增量转发，不重复）

### 🎭 会话管理

- 一个 QQ 会话可绑定任意 DSH 会话（默认一个会话持续用，可中途切换）
- 会话列表**带编号显示 DSH 真实标题**，`/dsh 选择 3` 即可切换
- 切换后默认**只转发新消息**（不刷历史），可用 `/dsh 历史` 查看
- 命令支持无空格写法：`/dsh创建` = `/dsh 创建`

### 📢 进度通知（核心）

- **插件初衷：DSH 干活时主动向用户汇报进度**
- `final`（默认）：只通知最终结果，安静不打扰
- `turn`：每轮完成 + 工具执行也通知
- `step`：每步进展都通知（最细粒度）
- 多会话时通知自动带上会话名，知道是哪个任务在汇报

### 🎯 触发方式（可配置）

- `at`：仅 @机器人 的消息转发（适合多人群，防止打扰）
- `all`：全部转发（适合自用群，只有你和 bot 最省事）
- `prefix`：指定前缀（如 `dsh:`）开头才转发

### 🧹 纯净模式

- **抛弃 AstrBot 人设**：桥接消息转发到 DSH 后自动阻止 AstrBot 主 LLM 回复，机器人不会用自带角色人设（如猫娘）干扰
- **极简 Agent 预设**：创建 DSH 会话时使用 `minimal` 预设，DSH 以纯粹 AI 助手身份回答
- **纯净指令**：可配置附加指令，明确要求 DSH 不进行角色扮演

### 🖼️ 图片与文件

- QQ 图片自动转为 base64 发送给 DSH
- QQ 文件自动保存到服务器，并告知 DSH 文件路径

### 🔐 权限控制

- 白名单群/用户控制（可配置）
- 管理员始终放行

## 📦 安装

```bash
git clone https://github.com/yvdi-abc/astrbot_plugin_dsh_bridge.git
```

将 `astrbot_plugin_dsh_bridge` 文件夹复制到 AstrBot 的 `data/plugins` 目录，重启 AstrBot 或在控制面板重载插件。

## 🚀 使用方法

### 控制命令

| 命令 | 说明 |
|------|------|
| `/dsh 帮助` | 显示帮助 |
| `/dsh 会话` | 列出 DSH 会话 |
| `/dsh 选择 <会话ID>` | 绑定当前QQ会话到指定DSH会话 |
| `/dsh 创建 [路径]` | 创建新DSH会话并绑定 |
| `/dsh 历史` | 查看当前会话最近历史 |
| `/dsh 状态` | 查看桥接状态 |
| `/dsh 发送 <内容>` | 手动发送消息到DSH |
| `/dsh 重命名 <名称>` | 给当前会话命名 |
| `/dsh 解绑` | 解除当前绑定 |

> 支持无空格写法：`/dsh创建` = `/dsh 创建`

### 使用流程

1. 在 QQ 群发送 `/dsh 创建` 创建专属 DSH 会话，或 `/dsh 会话` + `/dsh 选择 <ID>` 绑定已有会话
2. 绑定后，群内普通消息自动转发到 DSH
3. DSH 的新回复自动转发回群

## ⚙️ 配置说明

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enable` | bool | true | 插件总开关 |
| `dsh_base_url` | string | http://127.0.0.1:3080 | DSH 服务地址 |
| `agent_preset` | string | minimal | 创建会话用的 Agent 预设（minimal=极简/纯净） |
| `pure_mode` | bool | true | 纯净模式：转发后阻止主 LLM，避免机器人人设干扰 |
| `pure_mode_instruction` | text | 空 | 附加到 DSH 消息的纯净指令（可选） |
| `whitelist_enable` | bool | true | 是否启用白名单 |
| `whitelist_groups` | list | [] | 允许的群号列表 |
| `whitelist_users` | list | [] | 允许的用户QQ号列表 |
| `poll_interval` | int | 3 | DSH 回复轮询间隔（秒） |
| `auto_forward` | bool | true | QQ 消息自动转发到 DSH |
| `trigger_mode` | string | at | 触发方式：at=@机器人才转发，all=全部转发（自用群推荐），prefix=前缀触发 |
| `trigger_prefix` | string | dsh: | 触发前缀（trigger_mode=prefix 时生效） |
| `reply_forward` | string | plain | 回复方式：plain=纯文本分段，forward=合并转发 |
| `forward_threshold` | int | 500 | 超过此字符数才用合并转发 |
| `bot_name` | string | DSH助手 | 合并转发显示的 Bot 名称 |
| `enable_history_forward` | bool | false | 切换后是否默认转发历史 |
| `notify_mode` | string | final | 进度通知：final=只报结果（推荐），turn=每轮+工具，step=每步，off=不通知 |
| `notify_tool_result` | bool | true | 是否通知工具执行结果（turn/step 模式） |
| `notify_tool_result_max_len` | int | 200 | 工具结果通知最大字符数 |

## 🔧 工作原理

```
┌─────────┐  session.prompt   ┌──────────┐  自动转发  ┌─────────┐
│   DSH   │ ◄─────────────── │  AstrBot  │ ◄──────── │  QQ 群  │
│ (会话)   │ ───────────────► │  插件(桥)  │ ────────► │ /私聊   │
└─────────┘  session.history  └──────────┘  轮询转发   └─────────┘
```

- **QQ → DSH**：插件监听消息 → 调 `session.prompt` 注入 DSH 会话
- **DSH → QQ**：插件后台轮询 `session.history` → 检测新回复 → `context.send_message` 发回 QQ
- **增量转发**：记录每个会话已读的最大 seq，只转发新产生的回复

## 📝 注意事项

- 需要 DSH 运行在可访问的地址（默认本机 127.0.0.1:3080）
- DSH 的 `session.prompt` / `session.create` / `session.history` API 由本机直接调用
- 轮询间隔不宜过小（建议 ≥2 秒）
- 合并转发仅受 aiocqhttp (QQ) 平台支持
- 纯净指令仅在会话首条消息附加，避免重复消耗 token

## 🔄 更新日志

### v1.1.0
- ✨ **进度通知优化**：新增 `final` 模式（默认，只报最终结果，安静）；`turn`/`step` 可选
- ✨ **触发方式可配置**：`at`（@机器人才转发）/ `all`（全部转发，适合自用群）/ `prefix`
- ✨ 会话列表带编号：`/dsh 选择 3` 即可切换，不用复制完整 ID
- ✨ 多会话时通知自动带会话名，知道是哪个任务在汇报
- ✨ `notify_mode=off` 完全不通知（仅手动 `/dsh 历史` 查询）
- 📢 帮助文本显示当前触发/通知模式

### v1.0.2
- 🐛 **修复命令不触发**：AstrBot 唤醒前缀会剥离 `/`，导致 `on_message` 把 `/dsh` 命令当普通消息转发、主 LLM 用人设回复。现在 `dsh xxx`（剥离斜杠后）也能正确识别为命令
- 🐛 `cmd_dsh` 主动阻止主 LLM，命令消息不再被机器人人设回复
- ✨ **会话列表显示 DSH 真实标题**：从 `projections.values.title` 读取，一眼看出会话内容（如"查看SKILL.md文件内容"）
- ✨ 绑定会话时显示会话标题

### v1.0.1
- 🐛 修复无空格命令解析（`/dsh创建` = `/dsh 创建`）
- 🧹 纯净模式：转发后阻止 AstrBot 主 LLM 回复，抛弃机器人自带人设
- ✨ 创建会话支持 `agent_preset`（minimal 极简纯净模式）
- ✨ 纯净指令仅首条附加，避免重复浪费 token
- 🐛 修复 `/dshark` 等单词被误判为命令的问题（精确匹配）
- 🐛 修复发送失败仍更新已读 seq 导致消息丢失的问题（失败重试）
- ✨ 新增 `/dsh 重命名` 命令

### v1.0.0
- 初始版本：DSH ↔ QQ 双向桥接

## 📄 许可证

MIT License

## 👤 作者

**yvdi-abc**
