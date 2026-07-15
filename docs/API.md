# 接口文档 — wxbot_pyweixin

本文档描述 wxbot_pyweixin 的分层架构、对外 API、MQTT 任务接口、Webhook 与配置参考。
操作与运维指引见 [操作手册](./MANUAL.md)。

## 1. 分层架构

```
┌─────────────────────────────────────────────────────────┐
│  main.py / wxbot_gui.py        入口（CLI / GUI）         │
├─────────────────────────────────────────────────────────┤
│  wxbot/                        业务层                    │
│    monitor    消息主循环（双轨监听 + 异常自恢复）         │
│    reply      回复编排（关键词/转发/延时/记忆）          │
│    scheduler  定时消息/朋友圈/随机窗口/新好友/点赞       │
│    employee   数字员工编排（转人工→知识库→AI）           │
│    mqtt/      MQTT 数字员工通道（coordinator/worker/...）│
│    commands   admin /指令                                 │
│    config     BotConfig 配置管理（线程安全 + 热重载）    │
├─────────────────────────────────────────────────────────┤
│  pyweixin/                     底层 SDK（pywinauto UI）  │
│    WeChatAuto  消息/通讯录/文件/好友/朋友圈/监控/设置    │
│    WeChatTools Tools + Navigator                          │
│    Uielements  UI 元素 POM 封装                           │
└─────────────────────────────────────────────────────────┘
```

- 上层通过 `bot_config` 读取配置、通过全局单例驱动业务。
- 所有微信 UI 操作最终落到 `pyweixin` 的静态类方法。
- 多线程共享单一微信会话（`GlobalConfig.close_weixin = False`），UI 操作经 `ui_lock` 互斥。

## 2. pyweixin SDK 接口

`from pyweixin import ...` 导出：

| 类 | 职责 |
|----|------|
| `Messages` | 发送/拉取/搜索消息、会话导出 |
| `Files` | 发送文件、保存/导出媒体 |
| `Contacts` | 通讯录、好友资料、群成员、新好友检查 |
| `FriendSettings` | 加好友、改备注、删好友、拉黑、置顶、免打扰、权限 |
| `Moments` | 发朋友圈、笔记、导出/点赞朋友圈 |
| `Collections` | 收藏公众号文章、卡片链接转 URL |
| `Call` | 语音/视频通话 |
| `AutoReply` | 自动回复、监听聊天/新成员入群、抢红包 |
| `Monitor` | 新消息检查 |
| `Settings` | 登出、样式、语言、字体、下载体积、通知 |
| `Tools` | 微信运行检测、版本、窗口居中 |
| `Navigator` | 打开各类窗口（微信主界面/朋友圈/加好友面板/好友资料/独立会话） |
| `GlobalConfig` | 全局配置（`is_maximize` / `close_weixin` / `search_pages`） |

### 2.1 常用方法（分类，完整签名见 `pyweixin/WeChatAuto.py`）

**消息** (`Messages`)
- `send_messages_to_friend(friend, messages, at_members=[], close_weixin=None)` — 发文本（支持 @）
- `send_messages_to_friends(friends, messages, ...)` — 群发
- `send_audios_to_friend(friend, audios, audio_length=None, ...)` — 发语音
- `pull_messages(friend, number, ...)` — 拉取最近 N 条消息
- `dump_chat_history(friend, number, ...)` — 导出聊天记录
- `search_chat_history(keyword, number=None, ...)` — 搜索聊天记录
- `check_new_messages(...)` — 检查新消息
- `dump_recent_sessions(recent='Today', ...)` / `dump_sessions(...)` — 导出最近会话

**文件** (`Files`)
- `send_files_to_friend(friend, files, with_messages=False, messages=[], ...)` — 发文件（可附带消息）
- `save_chatfiles(friend, number, target_folder=None, ...)` — 保存聊天文件
- `save_media(friend, number, ...)` — 保存媒体
- `export_recent_files / export_videos / export_wxfiles(...)` — 按类型导出

**通讯录** (`Contacts`)
- `get_friends_info(...)` / `get_friends_detail(...)` — 好友列表 / 详情
- `get_friend_profile(friend, search_pages=None, ...)` — 单个好友资料（返回 dict：昵称/微信号/地区/备注/电话/标签/描述/朋友权限/共同群聊/个性签名/来源）
- `get_groups_info(...)` / `get_common_groups(friend, ...)` / `get_recent_groups(...)` — 群信息
- `get_groupMembers_info(group, ...)` — 群成员
- `check_new_friends(verify=False, limit=8, clear=False, ...)` — 检查/通过新好友申请
- `check_my_info(...)` — 当前账号信息（wxid / 微信号 / 昵称）

**好友设置** (`FriendSettings`)
- `add_new_friend(number, greetings=None, remark=None, chat_only=False, ...)` — 添加好友
- `change_remark(friend, remark, description=None, ...)` — 修改备注
- `delete_friend / block_friend / star_friend / mute_notification / pin_chat / clear_chat_history / change_privacy(friend, ...)`

**朋友圈** (`Moments`)
- `post_moments(text='', medias=[], ...)` — 发布图文朋友圈
- `post_notes(content=None, files=[], text=None, ...)` — 笔记发朋友圈
- `dump_recent_posts(recent='Today', ...)` / `dump_friend_posts(friend, number, ...)`
- `like_posts(recent='Today', ...)` / `like_friend_posts(friend, number, ...)`

**导航** (`Navigator`)
- `open_weixin(is_maximize=None)` / `open_moments(...)` / `open_add_friend_panel(...)` / `open_friend_profile(friend, ...)` / `open_seperate_dialog_window(friend)`

**工具** (`Tools`)
- `is_weixin_running()` / `get_weixin_version()` / `move_window_to_center(Window=...)`

> 所有 UI 方法均为 `@staticmethod`，默认 `close_weixin`/`is_maximize` 取 `GlobalConfig`。
> 本项目运行时统一 `close_weixin=False`（共享会话）。

## 3. wxbot 业务层接口

### 3.1 全局单例

| 单例 | 模块 | 说明 |
|------|------|------|
| `bot_config` | `wxbot.config` | 配置管理（线程安全） |
| `monitor` | `wxbot.monitor` | 消息主循环 |
| `scheduler` | `wxbot.scheduler` | 定时调度器 |
| `mqtt_worker` | `wxbot.mqtt.worker` | MQTT 数字员工 |
| `reply_engine` | `wxbot.reply` | 回复编排 |
| `input_blocker` | `wxbot.input_blocker` | 人工操作屏蔽 |
| `friend_add_ext` | `wxbot.friend_add` | 主动加好友扩展 |

### 3.2 配置 API — `BotConfig` (`wxbot/config.py`)

```python
from wxbot.config import bot_config

bot_config.load()                         # 加载（文件不存在则生成默认）
bot_config.reload()                       # 热重载
bot_config.cfg                            # dict：当前完整配置（快照）
bot_config.get(key, default=None)         # 读单项
bot_config.set(key, value, persist=True)  # 写单项（默认落盘）
bot_config.save()                         # 全量落盘
# 监听列表便捷操作（自动落盘）
bot_config.add_listen_user(name) / remove_listen_user(name)
bot_config.add_group(name) / remove_group(name)
```

默认配置见 `wxbot/config.py` 的 `DEFAULTS`；加载时经 `_normalize` 做范围校验与类型兜底。

### 3.3 联系人解析器 — `ContactResolver` (`wxbot/mqtt/resolver.py`)

将 wxid / 微信号 / 昵称 / 备注 解析为精确展示名与微信号。缓存落盘 `config/contacts_cache.json`。

```python
from wxbot.mqtt.resolver import ContactResolver, ResolveResult

r = ContactResolver()
res: ResolveResult = r.resolve("张三")
# res.success / res.display_name / res.matched_by(wxid|remark|nickname|substring) / res.wxid

res = r.find_wxid_by_remark("张三")   # 按备注查微信号：精确备注 > 备注子串(多候选取第一个)
# res.matched_by: remark | remark-substring

r.refresh_cache(timeout=120)          # 全量刷新（限速 60s）
r.cache_ready                         # bool：缓存是否非空
r.get_cache_info()                    # {size, age_seconds, cache_file}
r.get_all_contacts()                  # list[dict] 浅拷贝
r.add_contact(info)                   # 追加单个（按 wxid 去重）
r.update_or_add_by_remark(info)       # 按备注更新或新增
```

匹配优先级：`resolve` = 精确 wxid → 精确备注 → 精确昵称 → 子串唯一 → 子串多候选取最佳；
`find_wxid_by_remark` = 精确备注 → 备注子串（**多候选取第一个**）。

## 4. MQTT 任务接口

### 4.1 主题约定

每个 worker 身份（`config.mqtt_worker.workers[]`）配置 `topics`：

| 用途 | 主题模板 | 说明 |
|------|----------|------|
| 接收任务 | `agent/{role}/{agent_id}` (`subscribe`) | 上游下发的任务 |
| 回调结果 | `wechat/{role}/{agent_id}` (`callback_prefix`) | 回调主题 = `{callback_prefix}/{correlationId}` |
| 上行转发 | `wechat/{role}/{agent_id}` (`forward`) | 好友消息转发到上游 |

### 4.2 任务 event 类型

`TaskExecutor.TASK_METHOD_MAP`（`wxbot/mqtt/executor.py`）：

| event | 方法 | 说明 | 是否开窗 UI |
|-------|------|------|------------|
| `send_text` | `_execute_send_text` | 发文本/文件消息 | 是 |
| `wechat_message` | → `send_text` | 反向回复（含 targetName/targetId 时内部按 send_text 执行） | 是 |
| `add_friend` | `_execute_add_friend` | 添加好友 | 是 |
| `get_chat_history` | `_execute_get_chat_history` | 拉取聊天记录 | 是 |
| `get_friend_details` | `_execute_get_friend_details` | 好友详情（优先读缓存） | 视缓存 |
| `get_contacts_cache` | `_execute_get_contacts_cache` | 只读通讯录缓存 | 否 |
| `refresh_contacts` | `_execute_refresh_contacts` | 全量刷新通讯录 | 是 |
| `post_moments` | `_execute_post_moments` | 发布朋友圈 | 是 |
| `get_friend_moments` | `_execute_get_friend_moments` | 导出好友朋友圈（截图上传 MinIO） | 是 |
| `ping` | `_execute_ping` | 心跳 | 否 |

字段长度限制（`wxbot/mqtt/common.py`）：

| 常量 | 值 |
|------|----|
| `MAX_TARGET_LEN` | 128 |
| `MAX_MESSAGE_LEN` | 4096 |
| `MAX_VERIFY_TEXT_LEN` | 256 |
| `MAX_CONTACT_LEN` | 128 |
| `MAX_HISTORY_LIMIT` | 200 |
| `TASK_TIMEOUT_DEFAULT` | 300（秒） |

### 4.3 任务 payload

通用：所有任务可携带 `correlationId`（回调追踪）、`operate`（会话级操作标记，如 `manual`）。
字段同时支持「顶层」与「`params.` 嵌套」（兼容旧 taskType 格式）。

**send_text / wechat_message**
```jsonc
{
  "event": "send_text",
  "correlationId": "msg-001",
  "targetName": "张三",          // 或 "targetId": "wxid_xxx" / 微信号
  "text": "你好",                // 文本（≤4096）
  "fileUrl": "https://.../a.jpg", // 可选，文件 URL（下载后发送）
  "operate": "auto"              // 可选
}
```
发送成功后自动检测对方是否删除/拉黑自己；命中则 `wechatResult=false`、`wechatErrorType=deleted|blocked`，并触发飞书告警 + MQTT 系统事件（见 4.5）。

**add_friend**
```jsonc
{
  "event": "add_friend",
  "correlationId": "af-001",
  "targetName": "wxid_xxx",      // 微信号/手机号/wxid
  "verifyText": "我是…",         // 验证语（≤256）
  "remark": "张三-客户",         // 备注
  "tags": [],                    // 已知 gap：不支持，记日志跳过
  "permission": "public"         // 已知 gap：朋友圈权限不支持
}
```

**get_chat_history**
```jsonc
{ "event": "get_chat_history", "targetName": "张三", "limit": 50 }
// limit ≤ 200；targetName/targetId/contact 三者任一
```

**get_friend_details** / **get_contacts_cache** / **refresh_contacts**
```jsonc
{ "event": "get_friend_details", "targetName": "张三" }
{ "event": "get_contacts_cache" }     // 无参，读缓存
{ "event": "refresh_contacts" }       // 全量刷新
```

**post_moments**
```jsonc
{
  "event": "post_moments",
  "text": "今日推荐…",            // 文案
  "media_files": ["https://.../1.jpg"],  // 或 "images": ["url", ...]，≤9
  "privacy": "public",           // 已知 gap：仅 public 生效
  "tags": []                     // 已知 gap：不支持
}
// text 与 media_files 至少一项；配图全部失败且有文字 → 降级纯文字
```

**get_friend_moments**
```jsonc
{
  "event": "get_friend_moments",
  "targetName": "张三",
  "startDate": "2026-07-01",
  "endDate": "2026-07-15"
}
// 截图上传 MinIO，回调 event=moments_task_result
```

**ping**
```jsonc
{ "event": "ping", "correlationId": "p-1" }
// 返回 { "status": "success", "pong": true }
```

### 4.4 回调结果格式

任务执行后向 `{callback_prefix}/{correlationId}` 发布：

```jsonc
{
  "event": "task_result",        // get_friend_moments 为 "moments_task_result"
  "correlationId": "msg-001",
  "status": "success",           // success | error
  "result": { ... },             // 任务返回体
  "text": "<result 的 JSON 字符串>",
  "executedAt": "2026-07-15T10:00:00",
  "agentId": "wx_001",
  "role": "default",
  "senderId": "wxid_self",       // 实际微信号
  "operate": "auto",
  "targetId": "...", "targetName": "..."  // 透传原任务
}
```

### 4.5 上行消息转发（好友 → 上游）

好友发来消息时，`mqtt_worker.on_wechat_message(...)` 向 `forward` 主题发布：

```jsonc
{
  "event": "wechat_message",
  "correlationId": "wechat-<8hex>",
  "senderId": "wxid_friend", "senderName": "张三",
  "chatId": "张三", "chat": "张三",
  "text": "在吗",
  "operate": "auto",
  "agentId": "wx_001", "role": "default",
  "selfWxName": "我的昵称", "selfWxId": "wxid_self",
  "ts": 1721000000000
}
```

防回环：bot 刚发出的同文本消息（30s 内）不再转发（`is_recent_sent`）。

### 4.6 系统事件 — 好友不可达

发消息检测到被删/被拉黑，或 monitor 扫到系统回执时，发布 `wechat_contact_unreachable` 事件到 `forward` 主题：

```jsonc
{
  "event": "wechat_contact_unreachable",
  "correlationId": "unreachable-<8hex>",
  "chat": "张三", "type": "deleted",   // deleted | blocked
  "wechatErrorType": "deleted",
  "source": "send_text",                // send_text | monitor
  "messagePreview": "你好",
  "selfWxName": "...", "selfWxId": "...",
  "ts": 1721000000000
}
```
同一 (chat, type) 60s 内去重（`_alert_lock`）。

## 5. Webhook 接口

`wxbot/webhook_send.py`，配置文件 `config/webhook.json`。

```python
from wxbot import webhook_send
ok, msg = webhook_send.send_webhook(title="标题", content="内容")
ok, msg = webhook_send.send_message(title, content)  # 便捷封装
```

配置字段：`enabled` / `url` / `method` / `content_type` / `headers` / `body` / `timeout`。
`body` 支持 `$title`、`$content` 占位符；JSON body 先解析模板再渲染，避免运行时内容破坏 JSON。
对飞书/Lark 的 `code`/`StatusCode` 做二次校验（HTTP 200 但应用层拒绝时返回失败）。

## 6. 配置参考

完整字段见 `wxbot/config.py` 的 `DEFAULTS`。关键字段分组见 [操作手册 · 配置详解](./MANUAL.md#3-配置详解)。

主要分组：
- **监听**：`admin` / `AllListen_switch` / `listen_list` / `group` / `group_switch` / `black_list` / `chat_listen_only` / `group_listen_only`
- **关键词**：`chat_keyword_switch` / `group_keyword_switch` / `keyword_dict`
- **转发**：`custom_forward_switch` / `custom_forward_list` / `group_monitor_list` / `auto_collect_transfer` / `auto_open_red_packet`
- **新好友**：`new_friend_switch` / `new_friend_msg` / `new_friend_check_min/max` / `new_friend_remark_*`
- **定时**：`scheduled_msg_list` / `random_msg_list` / `scheduled_moments_list` / `random_moments_list`
- **朋友圈**：`moments_like_switch` / `moments_like_min/max` / `moments_post_pre_delay`
- **每日启停**：`everyday_start_stop_bot_switch` / `everyday_start_bot_time` / `everyday_stop_bot_time`
- **记忆**：`memory_switch` / `memory_max_count` / `memory_context_count`
- **数字员工**：`digital_employee_switch` / `api_configs` / `api_index` / `default_persona` / `chat_persona_map` / `group_persona_map` / `knowledge_switch` / `knowledge_threshold` / `escalation_*` / `customer_crm_switch`
- **好友添加**：`friend_add.{enabled,verify_text,rate_limit_seconds,daily_limit,retry_count,pre_delay}`
- **MQTT**：`mqtt_worker.{enabled,broker,task_timeout,throttle,minio,workers[]}`
- **人工屏蔽**：`input_block.{enabled,auto_release_minutes}`
- **运维**：`monitor_check_interval` / `monitor_run_timeout` / `contacts_refresh_timeout` / `reply_delay_min/max`
