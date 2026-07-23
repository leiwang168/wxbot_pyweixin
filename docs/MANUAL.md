# 操作手册 — wxbot_pyweixin

接口契约见 [接口文档](./API.md)。本文档面向部署与运维，覆盖安装、配置、指令、MQTT 接入与运维排障。

## 1. 环境要求

- **操作系统**：Windows 10/11 64-bit
- **微信**：4.1.x（已验证 4.1.9.35），需已登录
- **讲述人 trick**：已处理（UI 树可见，见 `pywechat/Weixin4.0.md`）
- **Python**：3.10+（需支持 TypeHint）
- **环境变量**：`set PYTHONUTF8=1`（中文/emoji 输出更稳）

## 2. 安装与运行

### 2.1 安装依赖

```powershell
pip install -r requirements.txt
```

### 2.2 命令行运行

```powershell
set PYTHONUTF8=1
python main.py
```

首次运行自动生成 `config/config.json`，按需编辑后重启或发 `/重载配置`。

### 2.3 图形界面

`wxbot_gui.py` 提供 tkinter 界面（可视化编辑配置、启停服务、实时日志）：

```powershell
set PYTHONUTF8=1
python wxbot_gui.py
```

### 2.4 打包为 exe

```powershell
# 1. 安装打包工具（PyInstaller 5.0+，验证 6.21.0）
pip install opencv-python pyinstaller

# 2. 按配置打包（onefile，无控制台，内置 Splash）
pyinstaller wxbot_gui.spec

# 3. 产物 dist/wxbot.exe，同级需放 config/ 目录
```

**打包资源**（`wxbot_gui.spec` 已配置）：

| 资源 | 路径 | 用途 |
|------|------|------|
| 模板图 | `config/images/*.png` | OpenCV 模板匹配（转账收款、红包拆开、弹框清理等按钮定位） |
| Splash | `config/images/splash.png` | 启动画面（建议 480×300） |

**必需模板图**（缺失则对应功能静默降级，不报错）：
- `shoukuan_btn.png` — 转账"收款"按钮
- `hongbao_btn.png` — 红包"开"按钮
- `confirm_btn.png` — 微信提示弹框"确定"按钮
- `unlock_btn.png`（可选）— 解锁提示按钮

打包后目录：
```
发布目录/
├── wxbot.exe
└── config/
    ├── config.json
    ├── persona/         # 岗位人设
    ├── knowledge.json   # FAQ
    └── images/          # 模板图
```

> `build/`、`dist/` 为打包产物（未纳入版本管理），重新打包前可删除。

## 3. 配置详解

配置文件 `config/config.json`，完整字段见 `wxbot/config.py` 的 `DEFAULTS`。加载时自动补全缺失字段并做范围校验。以下按功能分组。

### 3.1 监听模式

| 字段 | 默认 | 说明 |
|------|------|------|
| `admin` | `"文件传输助手"` | 管理员昵称（可发 `/指令`） |
| `AllListen_switch` | `false` | `false`=白名单模式；`true`=全局监听（黑名单）模式 |
| `AllListen_filter_mute` | `true` | 全局监听下过滤免打扰会话 |
| `black_list` | `[]` | 全局监听黑名单（私聊+群聊均生效） |
| `listen_list` | `[]` | 白名单用户（白名单模式生效） |
| `group` | `[]` | 监听群聊（白名单模式生效） |
| `group_switch` | `false` | 群聊监听总开关 |
| `chat_listen_only` | `false` | 私聊只监听不 AI 回复 |
| `group_listen_only` | `false` | 群聊只监听不回复 |
| `group_reply_at_msg` | `true` | 群回复是否 @ 发言人 |
| `group_reply_quote` | `false` | 群回复是否引用原消息（默认关闭，需运行时验证） |

### 3.2 关键词与转发

| 字段 | 说明 |
|------|------|
| `chat_keyword_switch` / `group_keyword_switch` | 关键词回复开关（私聊/群聊） |
| `group_keyword_at_only` | 群聊仅 @ 时触发关键词 |
| `keyword_dict` | `{关键词: 回复内容}` |
| `custom_forward_switch` / `custom_forward_list` | 自定义转发规则（type: keyword/all/sender；all_sources；forward_with_source） |
| `group_monitor_switch` / `group_monitor_list` | 群消息关键词监控（命中→读真实发送人→转发） |
| `auto_collect_transfer` | 收到好友转账自动确认收款 + 飞书提醒 |
| `auto_open_red_packet` | 收到红包自动拆开 + 飞书提醒 |

### 3.3 新好友

| 字段 | 默认 | 说明 |
|------|------|------|
| `new_friend_switch` | `false` | 自动通过好友申请 |
| `new_friend_reply_switch` | `false` | 通过后打招呼 |
| `new_friend_msg` | `[]` | 打招呼内容（文字或图片绝对路径） |
| `new_friend_check_min` / `max` | `60` / `300` | 检查间隔随机区间（秒） |
| `new_friend_remark_use_nickname` | `true` | 备注含昵称 |
| `new_friend_remark_prefix` / `suffix` | `""` / `"_机器人备注"` | 备注前后缀 |
| `new_friend_remark_prefix_timestamp` / `suffix_timestamp` | `false` | 前后缀追加日期 |
| `new_friend_tags` | `[]` | 已知 gap：不支持打标签，记日志跳过 |

> `check_new_friends` 有频率限制（单次≤8/每日≤4/间隔≥2h），调度取保守间隔。

### 3.4 定时任务

固定时刻（`scheduled_msg_list` / `scheduled_moments_list`）与随机窗口（`random_msg_list` / `random_moments_list`）。

```jsonc
"scheduled_msg_list": [{
  "id": "morning", "enabled": true, "targets": ["张三", "销售群"],
  "time": "08:30", "repeat_type": "weekly", "weekdays": [1,2,3,4,5],
  "msgs": ["早安！", "D:/pics/morning.png"]
}],
"random_msg_list": [{
  "id": "rand1", "enabled": true, "targets": ["李四"],
  "time_start": "09:00", "time_end": "21:00",
  "repeat_type": "weekly", "random_days_count": 3,
  "msgs": ["随机问候~"]
}]
```

- `repeat_type`：`once` / `daily` / `weekly` / `monthly` / `custom`
- 随机窗口：每日在 `[time_start, time_end]` 内预抽一个触发时刻，到点执行一次

### 3.5 朋友圈

| 字段 | 默认 | 说明 |
|------|------|------|
| `scheduled_moments_switch` / `random_moments_switch` | `false` | 定时/随机朋友圈开关 |
| `moments_like_switch` | `false` | 随机点赞活跃账号 |
| `moments_like_min` / `max` | `60` / `120` | 点赞间隔（分钟） |
| `moments_post_pre_delay` | `0` | 发朋友圈 UI 操作前延迟（秒），0=不延迟 |

### 3.6 每日启停 / 记忆 / 回复延时

| 字段 | 说明 |
|------|------|
| `everyday_start_stop_bot_switch` / `everyday_start_bot_time` / `everyday_stop_bot_time` | 每日定时停止/恢复监听（可跨夜） |
| `memory_switch` / `memory_max_count` / `memory_context_count` | 对话记忆开关与容量 |
| `reply_delay_switch` / `reply_delay_min` / `reply_delay_max` | 回复随机延时（秒） |

### 3.7 数字员工

| 字段 | 说明 |
|------|------|
| `digital_employee_switch` | 数字员工总开关（关闭则仅关键词/转发，不调 AI） |
| `api_configs` | OpenAI 兼容接口列表：`{sdk,key,url,model}`（DeepSeek/通义/智谱/OpenAI 通用） |
| `api_index` | 当前使用的接口索引 |
| `default_persona` | 全局默认岗位（对应 `config/persona/<名>.md`） |
| `chat_persona_map` / `group_persona_map` | 私聊/群聊专属岗位绑定 |
| `knowledge_switch` / `knowledge_threshold` | FAQ 知识库开关与模糊匹配阈值（0~1） |
| `escalation_switch` / `escalation_keywords` / `escalation_target` | 转人工：开关、触发词、通知对象（空=admin） |
| `customer_crm_switch` | 客户档案 CRM 开关 |

决策链：转人工 → 知识库（精确/模糊命中优先）→ AI（带客户档案上下文）。

### 3.8 好友添加扩展（主动加人）

```jsonc
"friend_add": {
  "enabled": false,
  "verify_text": "",          // 默认验证语
  "rate_limit_seconds": 60,   // 同一目标限流间隔
  "daily_limit": 20,          // 每日上限
  "retry_count": 3,           // 失败重试次数
  "pre_delay": 0              // UI 操作前延迟（秒）
}
```

### 3.9 MQTT 数字员工（OpenClaw 通道）

```jsonc
"mqtt_worker": {
  "enabled": false,
  "broker": { "host": "localhost", "port": 1883, "username": "", "password": "", "vhost": "/", "tls": false },
  "task_timeout": 10,
  "close_chat_window": true,
  "close_chat_window_delay": 1.0,
  "skip_local_reply_when_forwarded": true,
  "throttle": {
    "queue_max_size": 100,
    "rate_limit_per_second": 1.0,
    "rate_limit_burst": 3,
    "queue_alert_threshold": 80
  },
  "minio": { "endpoint": "", "access_key": "", "secret_key": "", "bucket": "wbot", "secure": true, "public_url_prefix": "" },
  "download_dir": "",
  "workers": [{
    "enabled": true, "role": "default", "agent_id": "wx_001",
    "topics": {
      "subscribe": "agent/{role}/{agent_id}",
      "callback_prefix": "wechat/{role}/{agent_id}",
      "forward": "wechat/{role}/{agent_id}"
    },
    "forward_contacts": []   // 空=兜底转发所有
  }]
}
```

### 3.10 人工操作屏蔽

```jsonc
"input_block": { "enabled": false, "auto_release_minutes": 30 }
```

`enabled=true` 时服务运行期间屏蔽人工对微信窗口的鼠标点击（把微信交给机器人），`Ctrl+Alt+X` 或 `/解除屏蔽` 解除，连续 N 分钟自动解除兜底。

### 3.11 运维相关

| 字段 | 默认 | 说明 |
|------|------|------|
| `monitor_check_interval` | `10` | 消息监听轮询间隔（秒） |
| `monitor_run_timeout` | `30` | 单轮 run_once 超时（秒），防 UI 死锁拖垮主循环 |
| `contacts_refresh_timeout` | `300` | 联系人全量缓存刷新超时（秒） |

## 4. admin 指令手册

仅 `config.admin` 发送的消息生效，前缀 `/`。

### 4.1 基础
```
/状态
/暂停私聊自动回复 | /恢复私聊自动回复
/暂停群聊自动回复 | /恢复群聊自动回复
/添加监听用户 名 | /删除监听用户 名
/添加群 名 | /删除群 名
/开关新好友 on|off
/关键词 on|off 私聊|群聊
/立即发朋友圈 文本|图片绝对路径[|图片...]
```

### 4.2 数字员工
```
/数字员工 on|off
/岗位列表 | /当前岗位 | /设置岗位 私聊|群聊 对象 岗位
/客户列表 | /客户档案 昵称 | /客户状态 昵称 新客户|跟进中|意向|已成交|已转人工 | /客户备注 昵称 内容
/转人工 on|off | /重载知识库
```

### 4.3 MQTT 通道
```
/员工状态 | /员工重连
```

### 4.4 好友添加
```
/添加好友 微信号或wxid
```

### 4.5 记忆与配置
```
/记忆列表 | /清空记忆 窗口名 | /清空全部记忆
/重载配置
```

### 4.6 人工屏蔽
```
/屏蔽微信 | /解除屏蔽（或 Ctrl+Alt+X）
```

`/重载配置` 会同步重载：配置、记忆设置、数字员工、MQTT、好友添加扩展。

## 5. MQTT 数字员工接入

1. 启动 MQTT broker（如 EMQX / Mosquitto），填 `mqtt_worker.broker`。
2. 配置 `workers[]`：每个身份一组 `role` / `agent_id` / `topics`。
3. `mqtt_worker.enabled = true`，重启或 `/重载配置`。
4. 上游向 `subscribe` 主题下发任务（event 模型，见 [API §4](./API.md#4-mqtt-任务接口)）。
5. 执行结果回调到 `{callback_prefix}/{correlationId}`。
6. 好友消息自动转发到 `forward` 主题。

**多角色约束**：最多 1 个兜底角色（`forward_contacts` 为空）；同一联系人不可同时出现在多个角色 `forward_contacts`（启动时告警）。

**限流**：`throttle.queue_max_size` 控制队列上限，超 `queue_alert_threshold` 触发飞书告警；`rate_limit_per_second` + `rate_limit_burst` 令牌桶限速。

## 6. 飞书 Webhook 配置

配置文件 `config/webhook.json`：

```jsonc
{
  "enabled": true,
  "url": "https://open.feishu.cn/open-apis/bot/v2/hook/<你的token>",
  "method": "POST",
  "content_type": "application/json",
  "headers": {},
  "body": "{\"msg_type\":\"text\",\"content\":{\"text\":\"$title\\n\\n$content\"}}",
  "timeout": 5
}
```

`$title` / `$content` 为占位符，运行时渲染。JSON body 先解析模板再渲染，避免内容破坏 JSON。触发场景：主循环异常、好友不可达、队列告警、转账/红包、新好友通过等。

## 7. 运维

### 7.1 日志

- 日志目录：`logs/`（按日期滚动，如 `wxbot_20260715.log`）
- MQTT 日志前缀 `[mqtt]`，好友添加 `[friend_add]`，朋友圈 `[朋友圈]`，主循环 `📨`

### 7.2 UI 锁与锁重建

微信 UI 操作经 `ui_lock` 互斥，期间 `monitor` 轮询、点赞、加好友等让位。

- **任务超时**（`task_timeout`）或单轮卡死时，`_rebuild_ui_lock` 重建锁 + 线程池 + Event。
- 卡死线程无法强杀，其恢复后通过**锁对象身份比较**发现自己持的锁已非当前活动锁，跳过所有副作用（不污染新任务），只释放自己持有的旧锁。
- 重建后 win32 强关残留朋友圈窗口。
- 重建由 `_rebuild_lock` 串行化，防止并发重建导致 epoch 双增与线程池泄漏。

### 7.3 主循环异常自恢复

- 单轮 `run_once` 在子线程执行，`join(timeout=monitor_run_timeout)` 兜底 UI 死锁。
- `_run_once_safe` 捕获单轮异常 + 飞书告警（夜间 23:00~06:00 静默，同异常 1 小时去重）。
- `monitor.loop` while 体内 `try/except` 兜底，单轮任意异常不中断主循环。
- `main.py` 主循环外层 `while True` + `except Exception`，意外退出 5s 后自动恢复并飞书告警；仅 Ctrl+C / 正常停止退出。

### 7.4 底层操作降速

`add_new_friend` / `post_moments` 在关键 UI 步骤间加入拟人等待，避免 UI 未就绪导致失败：
- 加好友：搜索回车后等 1.0s、`exists` 超时 2→4s、点添加后等 0.8s、确认后等 0.6s
- 发朋友圈：右键菜单后 0.6s、选文件各步 0.3–0.8s、文本输入后 0.4s

如需整体前置延迟，用 `friend_add.pre_delay` 与 `moments_post_pre_delay`。

### 7.5 联系人缓存

- 缓存文件：`config/contacts_cache.json`
- 启动时缓存为空自动全量拉取（`contacts_refresh_timeout` 超时）
- 全量刷新限速 60s（`_REFRESH_COOLDOWN`）
- 新好友通过后 `add_contact` 追加，避免全量刷新
- `find_wxid_by_remark` 按备注查微信号（精确 > 子串多候选取第一个）

### 7.6 人工操作屏蔽器

- `WH_MOUSE_LL` 低级钩子吞掉落在微信进程窗口的左键点击；机器人持 UI 锁操作期间（`_bot_active=True`）放行；键盘不拦。
- 解除优先级：`Ctrl+Alt+X` > `/解除屏蔽` > 杀进程兜底。
- 连续屏蔽 `auto_release_minutes` 分钟自动解除。

## 8. 常见问题

**Q: 启动报中文/emoji 乱码？**
A: 先 `set PYTHONUTF8=1` 再启动。

**Q: 主循环日志显示"单轮处理超时"？**
A: UI 操作卡死触发 `monitor_run_timeout` 兜底。检查微信窗口是否被遮挡/有弹窗；必要时调大 `monitor_run_timeout`。连续卡死会触发 UI 锁重建。

**Q: 加好友/发朋友圈偶发失败？**
A: 已在底层步骤间加入等待。仍有问题可调大 `friend_add.pre_delay` / `moments_post_pre_delay` 增加前置延迟。

**Q: 发消息后日志提示"检测到对方被删除/被拉黑"？**
A: 对方已删除或拉黑 bot，消息无法送达。系统会自动飞书告警 + 发布 `wechat_contact_unreachable` MQTT 事件，`wechatResult=false`。

**Q: MQTT 任务队列告警？**
A: 队列深度超 `queue_alert_threshold`（默认 80）。检查上游发送频率，或调大 `throttle.queue_max_size`。

**Q: 人工屏蔽后无法操作微信？**
A: `Ctrl+Alt+X` 解除，或发 `/解除屏蔽`，或等待 `auto_release_minutes` 自动解除。

## 9. 已知限制

1. pyweixin 无"好友打标签"接口 → `new_friend_tags` 记日志跳过。
2. `Moments.post_moments` 不支持隐私/标签 → 朋友圈隐私控制待评估扩展 SDK。
3. 群聊"引用原消息"能力待运行时验证 → `group_reply_quote` 默认 `false`。
4. 群消息真实发送人解析待精细化。
5. `check_new_friends` 有频率限制（单次≤8/每日≤4/间隔≥2h），调度取保守间隔。
6. MQTT password、MinIO access_key/secret_key 明文存 `config.json`（本地配置可接受；共享配置建议改环境变量）。

## 10. 经验教训与踩坑记录

> 本项目开发与运维中踩过的坑及沉淀的做法，供后续维护参考。

### 10.1 UI 自动化：坐标系必须统一
- **现象**：转账/红包 OpenCV 模板匹配后点击偏右；`click_input()` 点 ListItem 中心没点到消息卡片。
- **根因**：`pywinauto` UIA 的 `rectangle()` 返回**物理像素**，`PIL.ImageGrab`/`pyautogui` 在 DPI 未感知进程中用**逻辑**坐标，混用导致系统性偏移；对方消息卡片靠左，ListItem 几何中心落在空白区。
- **对策**：截图匹配优先用**全屏 `ImageGrab.grab()` + `pyautogui.click(max_loc + 模板/2)`**（PIL 与 pyautogui 同属 GDI 坐标系，参考 `_activate_weixin_by_cv`），不依赖 `rectangle()`；点击消息卡片按方向偏移（对方靠左点左 1/4）+ 换点重试（左→中→右）+ 点击后验证；入口尽早 `SetProcessDpiAwareness`。

### 10.2 MQTT：消费端必须按 correlationId 幂等去重
- **现象**：同一 `correlationId` 的发送任务被多次重投 → 好友收到重复消息。
- **根因**：QoS1「至少一次」+ 上游超时重试；`identity.is_duplicate`（TTL 去重）已实现却**从未被调用**。
- **对策**：入队层 `coordinator.enqueue_message` 调 `identity.is_duplicate(adapted)` 丢弃重复 cid。**写好的工具函数务必确认接入调用点，否则等于没写。**

### 10.3 剪贴板是进程级全局资源，必须串行化
- **现象**：发送消息偶发 `(1418, EmptyClipboard/CloseClipboard, 线程没有打开的剪贴板)`。
- **根因**：`copy_text_to_clipboard` 无 `try/finally`；`InputBlocker._notify`/`scheduler._send_msgs`/`friends.send_greeting` 等后台线程**绕过 UI 锁**直接操作剪贴板，与持锁的 executor 并发。
- **对策**：模块级 `_clipboard_lock` + `_with_clipboard(work)`（锁内 Open→work→Close，`try/finally` 保证关闭，1418 重试 3 次）。**一处加固覆盖所有调用方**，任何绕过 UI 锁的线程都共享这把锁。

### 10.4 底层 UI 操作必须留节奏
- **现象**：加好友/发朋友圈偶发失败（搜不到/按钮未就绪/请求丢失）。
- **根因**：步骤间无缓冲，UI 元素未渲染就操作。
- **对策**：底层（`WeChatAuto`）关键步骤间加 `time.sleep(0.3~1.0)` + `exists(timeout=)` 抬到 4s。上层 `pre_delay` 只控操作前等待，控不了「操作中」节奏——**节奏问题必须改底层**。

### 10.5 主循环是心脏，多层兜底防单点退出
- **对策**（`monitor.loop` / `main.py`）：单轮 `run_once` 放子线程 + `join(timeout)` 兜底 UI 死锁；`_run_once_safe` 捕获单轮异常 + 飞书告警（夜间静默、同异常 1h 去重）；`loop` while 体内再套 try/except；`main.py` 外层 `while True`+`except Exception` 意外退出 5s 自动恢复。**成本极低，收益是「永不静默死亡」。**

### 10.6 锁重建：Python 杀不掉线程，靠身份比较自检
- **现象**：UI 卡死后重建锁，旧线程恢复时误清新任务 event / 误操作 UI。
- **根因**：Python 无法强制 kill 线程。
- **对策**（`coordinator._rebuild_ui_lock`）：换新 Lock+新 Event+epoch+1；旧线程 `_exit` 用**锁对象身份比较**（`lock is cur_lock`）判断是否仍是活动锁，不是则跳过所有副作用、只释放自己的旧锁，**绝不误释放新锁**。

### 10.7 跨线程共享状态要封装加锁
- **现象**：`_last_sent` 防回环指纹由协调器写、转发读，跨线程裸 dict 访问。
- **对策**：封装为 `record_last_sent()`/`is_recent_sent()`，内部 `_operate_lock` 保护。**不要让外部直接 `obj._dict[k]=v`。**

### 10.8 微信「附属内容」常是独立列表项，且有时延
- **现象**：语音转文字读不到/读到空。
- **根因**：语音转文字结果是**下一条独立 ListItem**，且转换有延迟，收到语音时下一条可能未出现。
- **对策**（`read_chat_messages`）：语音读下一条；若非文本（未完成）按 `voice_message_delay` 等待后**重读**，**只在未就绪时才等待**避免无谓阻塞。

### 10.9 GUI 嵌套字段：变量前缀要避开通用保存循环
- **现象**：GUI 把 minio/workers 字段变量存进 `_bool_vars`/`_str_vars`，通用循环误当顶层 config 字段写入（污染）。
- **对策**：嵌套字段变量统一前缀（`minio_*`/`wk_*`/`mqtt_*`/`wh_*`/`ib_*`），通用循环按前缀跳过，单独组装回嵌套结构。

### 10.10 微信输入框不要直接 set_text
- **根因**：mmui 输入框直接 `set_text` 相当于默认 clear，行为不稳。
- **对策**：`Messages.send_messages_to_friend` 走 `copy_text_to_clipboard` + `Ctrl+V` + `Alt+S`（配合 10.3 的剪贴板锁）。

## 11. 目录结构

```
wxbot_pyweixin/
├── main.py              # 入口
├── wxbot_gui.py         # GUI
├── pyweixin/            # 内嵌 SDK
├── config/              # 运行时配置（config.json / persona / knowledge.json / webhook.json / images / contacts_cache.json）
├── memory/<wxid>/       # 对话记忆
├── logs/                # 运行日志
├── docs/                # 本文档
├── openclaw/            # openclaw agent 示例
└── wxbot/
    ├── config.py        # BotConfig
    ├── monitor.py       # 消息主循环
    ├── reply.py         # 回复编排
    ├── friends.py       # 新好友自动通过
    ├── moments.py       # 朋友圈发布/点赞
    ├── scheduler.py     # 定时调度
    ├── commands.py      # /指令
    ├── memory.py        # 对话记忆
    ├── ai_base.py / ai_openai.py   # AI
    ├── persona.py       # 岗位人设
    ├── knowledge.py     # FAQ 知识库
    ├── customer.py      # 客户档案 CRM
    ├── employee.py      # 数字员工编排
    ├── input_blocker.py # 人工操作屏蔽
    ├── friend_add.py    # 主动加好友
    ├── webhook_send.py  # 飞书通知
    └── mqtt/            # MQTT 数字员工通道
```
