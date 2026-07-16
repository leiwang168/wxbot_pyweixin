# 架构设计

## 1. 分层架构

```
┌───────────────────────────────────────────────────────────┐
│  main.py / wxbot_gui.py         入口（CLI / GUI / exe）     │
│  ├ 账号解析(wxid) → 配置加载 → 初始化(MQTT/屏蔽器/数字员工) │
│  └ 启动 monitor.loop()(前台) + scheduler(后台线程)         │
├───────────────────────────────────────────────────────────┤
│  wxbot/                         业务层                      │
│   monitor     消息主循环（双轨监听 + 异常自恢复 + 转账红包） │
│   reply       回复编排（关键词/转发/延时/记忆）             │
│   scheduler   定时/随机任务 + 新好友 + 点赞（后台线程）      │
│   employee    数字员工编排（转人工→知识库→AI）              │
│   commands    admin /指令                                    │
│   mqtt/       MQTT 数字员工通道（coordinator/worker/...）   │
│   friend_add  主动加好友扩展                                 │
│   input_blocker  人工操作屏蔽（WH_MOUSE_LL 钩子）           │
│   config      BotConfig（线程安全 + 热重载）                │
│   webhook_send   飞书/Webhook 通知                          │
├───────────────────────────────────────────────────────────┤
│  pyweixin/                      底层 SDK                    │
│   WeChatAuto  消息/通讯录/文件/好友/朋友圈/监控/设置(静态类)│
│   WeChatTools Tools + Navigator                              │
│   WinSettings 剪贴板/音量/监听模式（含 _clipboard_lock）    │
│   Uielements  UI 元素 POM 封装                               │
└───────────────────────────────────────────────────────────┘
```

- 上层通过 `bot_config` 读配置、通过全局单例（`monitor`/`scheduler`/`mqtt_worker`/`input_blocker`/`friend_add_ext`）驱动业务。
- **所有微信 UI 操作最终落到 `pyweixin` 的静态类方法**，共享单一微信会话（`GlobalConfig.close_weixin=False`）。
- 多线程操作 UI 经 `ui_lock` 互斥；剪贴板操作经 `_clipboard_lock` 串行化。

## 2. 线程模型

| 线程 | 入口 | 职责 | 异常兜底 |
|------|------|------|----------|
| **主线程** | `monitor.loop` | 消息主循环（前台） | `_run_once_safe` + `loop` try + `main.py` 自动恢复 |
| MonitorRunOnce（daemon） | `_run_once_guarded` | 单轮 run_once（子线程 + join timeout 防 UI 死锁） | 单轮 try/except + 飞书告警 |
| new_friend（daemon） | `friends.new_friend_loop` | 自动通过好友申请 | 循环内 try/except |
| moments_like（daemon） | `moments.like_loop` | 随机点赞 | 循环内 try/except |
| scheduler_tick（daemon） | `Scheduler.tick` | 定时/随机任务调度 | 循环内 try/except |
| MqttCoordinator（daemon） | `_run_event_loop` | MQTT 连接 + 任务出队 + 线程池派发 | 重连退避 + 任务超时锁重建 |
| TaskPool（2 worker） | `ThreadPoolExecutor` | 执行 MQTT 任务（持 UI 锁） | future.result(timeout) + 超时重建 |
| InputBlockerHook（daemon） | `_hook_loop` | 低级鼠标钩子消息循环 | 消息循环内处理 |
| InputBlockerNotify（daemon） | `_notify` | 向文件传输助手发通知 | try/except |

> 所有后台线程均为 daemon，进程退出自动回收；Python 无法强制 kill 线程，卡死线程通过**锁重建 + 身份比较**隔离（见 [经验教训 10.6](../MANUAL.md#106-锁重建python-杀不掉线程靠身份比较自检)）。

## 3. 关键数据流

### 3.1 收到好友消息 → 转发上游 / 本地回复

```
微信UI → monitor.run_once 扫描会话列表
  → read_chat_messages (classify_message 识别类型/方向)
  → _is_self_message (像素判方向，过滤自己消息)
  → 防回环 is_recent_sent (record_last_sent 指纹比对，30s)
  → _process_one:
      ├ 转账/红包 → _confirm_transfer / _open_red_packet
      ├ admin /指令 → commands.handle
      ├ 群监控关键词 → _group_monitor_forward
      └ 数字员工编排 employee (转人工→知识库→AI) → reply_engine 回复
  → mqtt_worker.on_wechat_message 转发到上游 forward topic
```

### 3.2 MQTT 任务下发 → 执行 → 回调

```
上游 publish → subscribe topic
  → MqttAdapter._on_message
  → coordinator.enqueue_message:
      ├ _adapt_payload (JSON 校验 / taskType→event 映射)
      ├ is_self_target (防自指向)
      ├ is_duplicate (correlationId 去重, 300s)   ← 防重复执行
      └ PriorityQueue.put (优先级 + 序号)
  → _run_loop_inner 出队 → TokenBucket 限速
  → _process_task → ThreadPoolExecutor.submit(execute_task)
      ├ executor 持 ui_lock (_enter_ui) 操作微信
      └ 释放 (_exit_ui，身份比较防误清)
  → 回调 task_result → callback_prefix/{correlationId}
```

### 3.3 转账自动收款

```
收到"微信转账"消息 → _confirm_transfer
  → 换点点击消息卡片（左1/4→中→右1/4，验证详情窗口弹出）
  → 详情窗口全屏截图 → OpenCV 匹配 shoukuan_btn.png
  → pyautogui.click(中心) 收款 → Esc 关闭结果
```

## 4. 并发与锁矩阵

| 锁 | 位置 | 保护对象 |
|----|------|----------|
| `ui_lock`（可重建） | `coordinator._ui_lock` | 微信 UI 操作互斥（executor / monitor / 朋友圈 / 加好友） |
| `_ui_tls`（线程局部） | `worker`/`executor` | 记录持锁归属，身份比较 |
| `_rebuild_lock` | `coordinator` | 串行化锁重建，防 epoch 双增 |
| `_clipboard_lock` | `WinSettings`（模块级） | 所有剪贴板 Open/Empty/Set/Close 串行化（防 1418） |
| `_operate_lock` | `worker` | `_session_operate` / `_last_sent` 读写 |
| `_alert_lock` | `worker` | 好友不可达告警去重 |
| `BotConfig._lock` | `config` | 配置读写 |
| `ContactResolver._lock` | `resolver` | 联系人缓存读写 |
| `input_blocker._lock` | `input_blocker` | 屏蔽状态 / bot_active |

## 5. 核心设计决策

详见 [操作手册·第 10 章 经验教训](../MANUAL.md#10-经验教训与踩坑记录)，要点：

- **UI 坐标系统一**：截图匹配优先全屏 `ImageGrab` + `pyautogui.click`，绕开 pywinauto 物理坐标与 DPI 错位。
- **MQTT 幂等**：`identity.is_duplicate` 在入队层按 cid 去重（QoS1 至少一次 + 上游重试）。
- **主循环多层兜底**：子线程 join + 单轮 try + 外层自动恢复，永不静默死亡。
- **锁重建身份比较**：卡死线程无法 kill，靠 `lock is cur_lock` 自检跳过副作用。
- **剪贴板进程级锁**：一处加固覆盖 executor / 通知 / 调度 / 打招呼所有调用方。
