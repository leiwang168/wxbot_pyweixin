# 变更日志

> 仅记录近期重大改动。完整提交历史见 `git log`。

## 2026-07 韧性加固与能力补全

### 底层 UI 节奏与查询能力
- **加好友/发朋友圈底层降速**：`WeChatAuto.add_new_friend` / `post_moments` 关键步骤间加入拟人等待（0.3~1.0s），`exists` 超时 2→4s，避免 UI 未就绪失败。
- **按备注查微信号**：`ContactResolver.find_wxid_by_remark()`（精确备注 > 备注子串多候选取第一个）。

### 异常韧性（主干不中断）
- `monitor.loop` while 体内加 `try/except` 兜底，单轮任意异常不终止主循环。
- `main.py` 主循环外层 `while True` + `except Exception`，意外退出 5s 自动恢复 + 飞书告警。
- `coordinator._process_task` 的 `submit` 异常防护（`result` 初始化，避免不必要 MQTT 重连）。

### 互斥加固
- `_last_sent` 防回环指纹封装为 `record_last_sent()` / `is_recent_sent()`（`_operate_lock` 保护）。
- `input_blocker.set_bot_active` 加锁，与 `enable/disable` 互斥。

### 转账/红包/去重/剪贴板
- **转账/红包点击卡片**：`msg_item.click_input()`（ListItem 几何中心落空）改为按 `rectangle` 换点（左 1/4→中→右 1/4）+ 点击后验证重试。
- **消息去重**：`coordinator.enqueue_message` 接入 `identity.is_duplicate`（cid TTL=300s），杜绝上游/QoS1 重投导致的重复发送。
- **剪贴板竞态加固**：`WinSettings` 新增 `_clipboard_lock` + `_with_clipboard(work)`（进程级串行化 + try/finally + 1418 重试），覆盖 6 个剪贴板方法。

### 被删/拉黑检测
- 发消息后检测对方删除/拉黑，飞书告警 + MQTT `wechat_contact_unreachable` 系统事件回执（60s 去重）。

### 语音消息
- 收到语音消息若转文字未就绪，按 `voice_message_delay`（默认 5s，可配置）等待后重读下一条。

### GUI
- **配置系统重构**：嵌套 Notebook 分组（基本信息 / MQTT / Webhook / 高级设置），minio/workers 每字段独立控件；变量前缀（`minio_*/wk_*`）避开通用保存循环污染。
- **人工屏蔽修复**：`wxbot_gui._start_service` 补 `input_blocker.start()/enable()`（此前 GUI 模式从未装钩子）。
- `voice_message_delay` 进入「高级设置 → 运行参数」。

### 文档
- 新增 `docs/API.md`（接口文档）、`docs/MANUAL.md`（操作手册 + 经验教训）。
- 新增 `docs/wiki/`（Home / Overview / Architecture / Modules / Development / Changelog）。
- README 增加「📚 文档」索引与「阶段三」能力。

## 已知遗留
- `no active desktop`（锁屏/RDP 断开/服务会话下 pyautogui 失败）：环境性问题，已被 `_run_once_safe` 兜底，未根除。
- `Moments.post_moments` 不支持隐私/标签；好友打标签无 SDK 接口。
- 群聊「引用原消息」与真实发送人解析待精细化。
