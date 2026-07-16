# 模块清单

## pyweixin/（底层 SDK，内嵌自 pywechat）

| 文件 | 职责 |
|------|------|
| `WeChatAuto.py` | 主模块：`Messages`/`Contacts`/`Files`/`FriendSettings`/`Moments`/`Collections`/`Call`/`AutoReply`/`Monitor`/`Settings` 等静态类 |
| `WeChatTools.py` | `Tools`（微信运行检测/版本/窗口居中）+ `Navigator`（打开各类窗口） |
| `WinSettings.py` | 剪贴板（`_clipboard_lock` 串行化）/ 音量 / 监听模式 |
| `Uielements.py` | UI 元素 POM 封装（kwargs，供 pywinauto 定位） |
| `utils.py` | `classify_message`（消息分类）/ `detect_block_or_delete`（被删拉黑检测）/ 消息解析辅助 |
| `Config.py` | `GlobalConfig`（is_maximize / close_weixin / search_pages） |
| `Errors.py` | `NoSuchFriendError` / `NotFriendError` 等 |

## wxbot/（业务层）

### 入口与核心
| 文件 | 职责 |
|------|------|
| `monitor.py` | 消息主循环：`loop` / `_run_once_guarded` / `read_chat_messages` / `_process_one` / 转账 `_confirm_transfer` / 红包 `_open_red_packet` / 群监控 `_group_monitor_forward` |
| `config.py` | `BotConfig` 单例：`DEFAULTS` / `_normalize` / 热重载 / 监听列表便捷操作 |
| `reply.py` | `reply_engine`：回复编排（关键词/转发/延时/记忆/AI 触发） |
| `scheduler.py` | `Scheduler` + `_RandomTaskRunner`：定时/随机任务，启动新好友与点赞线程 |
| `commands.py` | admin `/指令` 分发 |
| `logger.py` | 日志（文件 + 控制台） |

### 数字员工
| 文件 | 职责 |
|------|------|
| `employee.py` | 数字员工编排：转人工 → 知识库 → AI 三级决策 |
| `ai_base.py` / `ai_openai.py` | AI 抽象基类 / OpenAI 兼容实现 |
| `persona.py` | 岗位人设（`config/persona/<岗位>.md`） |
| `knowledge.py` | FAQ 知识库（精确 + 关键词重合度模糊） |
| `customer.py` | 客户档案 CRM（建档/状态流转/跟进记录） |
| `memory.py` | 对话记忆（`memory/<wxid>/<chat>` 文件化） |

### MQTT 数字员工通道
| 文件 | 职责 |
|------|------|
| `mqtt/worker.py` | `MqttWorkerExtension` 单例：生命周期 / 账号信息 / 上行转发 / 防回环 / 不可达告警 |
| `mqtt/coordinator.py` | `MqttCoordinator`：MQTT 连接 / 优先队列 / 令牌桶 / UI 锁重建 / 任务派发回调 |
| `mqtt/executor.py` | `TaskExecutor`：9 类任务执行（`TASK_METHOD_MAP`）/ 发送后删拉黑检测 |
| `mqtt/identity.py` | `WorkerIdentity`：身份配置 / `is_duplicate` cid 去重 / `is_self_target` |
| `mqtt/resolver.py` | `ContactResolver`：联系人缓存 / `resolve` / `find_wxid_by_remark` |
| `mqtt/adapter.py` | `MqttAdapter`：paho-mqtt 封装 |
| `mqtt/common.py` | 常量（`DEDUP_WINDOW` / `MAX_*_LEN` / 优先级映射）+ `TokenBucket` |

### 其他
| 文件 | 职责 |
|------|------|
| `friend_add.py` | 主动加好友扩展（限流/配额/重试/锁） |
| `friends.py` | 新好友自动通过 + 改备注 + 打招呼 |
| `moments.py` | 朋友圈发布 / 点赞（UI 锁上下文） |
| `input_blocker.py` | 人工操作屏蔽（WH_MOUSE_LL 钩子 + Ctrl+Alt+X） |
| `webhook_send.py` | 飞书 / 通用 Webhook 通知 |
| `wx_dialog.py` | 微信弹窗 OpenCV 清理 |
| `pending_friends.py` | 待通过好友标记 |
| `paths.py` | 配置/日志/记忆目录解析 |
| `group_monitor.py` | 群消息关键词监控辅助 |

## 模块依赖（高层）

```
main / wxbot_gui
  └─ monitor ── reply ── employee ── (ai_openai / persona / knowledge / customer / memory)
       │            └─ mqtt/worker ── mqtt/coordinator ── mqtt/executor
       │                                  └─ mqtt/resolver (contacts_cache.json)
       ├─ scheduler ── moments / friends
       ├─ commands ── (friend_add / input_blocker / webhook_send / customer / persona)
       └─ config (bot_config 单例，全局共享)
所有 UI 操作 → pyweixin (WeChatAuto / WinSettings / Navigator)
```
