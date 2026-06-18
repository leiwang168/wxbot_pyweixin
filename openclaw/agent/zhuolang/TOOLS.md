# TOOLS.md - zhuolang 技术规范

## MQTT 通信配置

### 连接参数
- **Broker**: x.x.x.x:1883 （以 `config/mqtt.yaml` 为准）
- **Auth**: xxx / xxx （以 `config/mqtt.yaml` 为准）
- **出站Topic（按实例隔离）**: 每个 Wbot 实例有独立的下行 topic
  - `agent/zhuolang/wx_001` — wx_001 专属销售A
  - `agent/zhuolang/wx_002` — wx_002 专属销售B
  - 未在 instance 配置 `outbound_topic` 时回退到全局 `agent/zhuolang`（向后兼容）
- **回调Topics**: wechat/wx_001/+, wechat/wx_002/+ (按实例隔离，支持多实例)
- **Agent ID**: zhuolang (OpenClaw agent)
- **Wbot 实例**: wx_001, wx_002, ... (配置文件管理)

### 多实例隔离机制

一个 OpenClaw agent 同时驱动多个 Wbot 时，靠 **per-instance MQTT topic** 做物理隔离，不依赖字段路由：

| 方向 | 隔离方式 | 说明 |
|------|----------|------|
| 下行（Agent → Wbot） | 每实例独立 `outbound_topic` | `ProcurementAgent` 发布到 `agent/zhuolang/{instance_id}`，仅订阅该 topic 的 Wbot 收到，避免重复执行 |
| 上行回调（Wbot → Agent） | 每实例独立 `callback_prefix` | Wbot 发布到 `wechat/{instance_id}/{cid}`，`ProcurementAgent` 订阅 `wechat/{instance_id}/+` 按 `correlationId` 匹配 |
| 上行消息（微信 → Agent） | topic 前缀 + session key | `mqtt_listener.py` 从 topic 反解 instance_id，session = `agent:zhuolang:wechat:{instance_id}:{联系人}` |

> ⚠️ Wbot 侧必须把 `subscribe_topic` 配成对应的 `agent/zhuolang/{instance_id}`，否则任务无人接收。
>
> `params.instance_id` 字段仍会注入，但仅作日志/调试用途，**不再承担路由职责**。

## 工具清单

| 工具 | 路径 | 用途 |
|------|------|------|
| mqtt_client | tools/mqtt_client.py | MQTT 通信核心（底层API） |
| send_inquiry | tools/send_inquiry.py | 发送消息/邀请 |
| fetch_quotes | tools/fetch_quotes.py | ~~拉取聊天记录~~ 已暂停使用 |
| follow_up | tools/follow_up.py | 回复微信联系人 |
| tesseract_ocr | tools/tesseract_ocr.py | 通用image解析工具 |
| analyze_quote | tools/analyze_quote.py | 通用Excel解析工具 |
| mqtt_listener | tools/mqtt_listener.py | 后台常驻监听器（微信+小程序） |
| send_msg_with_file | tools/send_msg_with_file.py | 消息+文件发送 |
| upload_to_minio | tools/upload_to_minio.py | 文件上传到MinIO |
| add_friend | tools/add_friend.py | 通过MQTT添加微信好友 |
| contact_instance | tools/contact_instance.py | 列出可用实例 |
| watchdog | tools/watchdog.py | 监听器守护进程 |
| reply_to_app | tools/reply_to_app.py | 回复小程序消息 |
| app_listener | tools/app_listener.py | 小程序MQTT监听器（独立进程） |
| _download_image | tools/_download_image.py | 通过MQTT获取图片（内部工具） |
| _get_file | tools/_get_file.py | 通过MQTT获取文件（内部工具） |
| _content_check | tools/_content_check.py | 消息合规检查（内部工具） |
| _config | tools/_config.py | 配置加载器（内部工具） |
**MQTT 任务类型（通过 agent 调用）：**
- `send_text` — 发送消息（支持 fileUrl 附件）
- `add_friend` — 添加好友（支持 remark/tags/permission）
- `get_chat_history` — 拉取聊天记录
- `get_friend_details` — 获取好友详情列表
- `post_moments` — 发朋友圈（支持图片/视频）
- `ping` — 测试连通性

## 核心原则

### ⚠️ 不要直接调用底层API
- ❌ **不要直接使用** `mqtt_client.py` 的 `send_text()` / `get_chat_history()`
- ✅ **统一使用** `tools/` 目录下的命令行工具
- 原因：底层API错误处理不完善，容易因微信窗口焦点问题失败

### ⚠️ 必须指定 `--instance` 参数
- 所有工具都需要手动指定 `--instance` 参数（如 `--instance wx_001`）
- 不再支持根据微信联系人自动查找实例

### ❌ 不要用 python -c 调用
- `python -c "from tools.mqtt_client import ..."` 方式容易失败
- 需要写临时脚本才能正常工作

## 工具使用规范

### 1. 发送消息
```bash
python tools/follow_up.py 微信联系人 "消息内容" --instance wx_001 --json
```

**返回结果：**
```json
{"ok": true, "cid": "xxx", "error": "", "contact": "李铎TS", "instance": "wx_001"}
```

### 2. ~~拉取聊天记录（已暂停使用）~~
~~`tools/fetch_quotes.py` — 暂不调用~~

### 2. 发送消息+附件
```bash
# 发送本地文件 + 消息给微信联系人
python tools/send_msg_with_file.py "微信联系人" "消息内容" "文件路径" --instance wx_001

# 发送网络图片/文件 + 消息给微信联系人（http/https 跳过上传，直接发）
python tools/send_msg_with_file.py "微信联系人" "消息内容" "https://minio.example.com/bucket/photo.jpg" --instance wx_001
```

### 3. 添加微信好友
```bash
python tools/add_friend.py 17729292473 --remark "备注名" --verify-text "你好" --tags "标签" --permission 朋友圈 --instance wx_001
```

### 4. 查询实例/微信联系人
```bash
python tools/contact_instance.py --list
python tools/contact_instance.py --instance wx_001 "微信联系人名"
```

### 5. 回复小程序消息
```bash
python tools/reply_to_app.py <correlationId> "回复内容"
```

### 6. 发布朋友圈
```python
from tools.mqtt_client import ProcurementAgent
agent = ProcurementAgent()
agent.connect()

# 纯文字朋友圈
cid, result = agent.post_moments(text="今天天气真好！")

# 图文朋友圈
cid, result = agent.post_moments(
    text="新品到货 🍺",
    media_files=["https://minio.example.com/bucket/photo1.jpg"],
)
```

### 7. 守护进程
```bash
# 启动（自动检测+重启监听器）
python tools/watchdog.py --interval 30

# 单次检测
python tools/watchdog.py --once
```

## MQTT Client API（仅供工具内部使用）

以下API仅供 `tools/` 目录下的工具调用，不要在业务逻辑中直接使用：

```python
from tools.mqtt_client import ProcurementAgent

# 创建指定实例的 agent
agent = ProcurementAgent(instance_id='wx_001')
agent.connect()

# 发送文本消息
cid, result = agent.send_text('微信联系人名', '消息内容')

# 发送带文件的消息
cid, result = agent.send_text('微信联系人名', '消息内容', file_url='https://...')

# 拉取聊天记录
cid, result = agent.get_chat_history('微信联系人名', limit=50)

# 发朋友圈
cid, result = agent.post_moments(text='文字', media_files=['https://...'])

# Ping测试
result = agent.ping(timeout=10)

agent.disconnect()
```

## 监听器管理

### 启动守护进程（推荐）
```bash
python tools/watchdog.py --interval 30
```
守护进程自动启动监听器，检测到掉线自动重启。

### 手动启动监听器
```bash
python tools/mqtt_listener.py
```

### 检查运行状态
```bash
Get-Process python | Where-Object {$_.Path -like "*mqtt_listener*"}
Get-Process python | Where-Object {$_.CommandLine -like "*watchdog*"}
```

### 检查日志
```bash
Get-Content logs\mqtt_listener.log -Tail 50
Get-Content logs\watchdog.log -Tail 20
```

### 测试MQTT连接
```bash
python -c "from tools.mqtt_client import ProcurementAgent; a=ProcurementAgent(); print(a.connect())"
```

## Session 隔离

### Session 结构
- `agent:zhuolang:wechat:{instance_id}:{微信联系人名}` — 每个微信联系人独立session
- `agent:zhuolang:wechat:{instance_id}:default` — 未知联系人兜底session
- `agent:zhuolang:app` — 小程序消息session
- `main` — 主session

### 消息路由
- `mqtt_listener.py` 自动按sender和instance_id路由到对应session
- 不会串上下文

## 故障排查

### 问题1：收不到微信消息
```bash
Get-Process python | Where-Object {$_.Path -like "*mqtt_listener*"}
Get-Content logs\mqtt_listener.log -Tail 50
```

### 问题2：发不出去消息
1. 检查Wbot是否在线
2. 检查微信窗口焦点
3. 使用 tools/follow_up.py 而非直接调用mqtt_client

### 问题3：监听器挂了
守护进程会自动拉起。检查 watchdog 日志：
```bash
Get-Content logs\watchdog.log -Tail 20
```
