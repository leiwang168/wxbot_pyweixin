# 开发指南

## 1. 开发环境

- Windows 10/11 64-bit，微信 4.1.x 已登录
- Python 3.10+
- `set PYTHONUTF8=1`（中文/emoji 输出）
- `pip install -r requirements.txt`
- 讲述人 trick 已处理（UI 树可见）

## 2. 调试技巧

- **日志**：`logs/wxbot_<date>.log`，前缀区分模块（`📨` 主循环 / `[MqttWorker]` / `[friend_add]` / `[朋友圈]`）。GUI 有「日志」页实时滚动。
- **干跑单功能**：参考 `test/` 下的脚本（如 `test_get_contacts_cache.py`、`like_latest_moment.py`），单独验证某个能力。
- **UI 定位**：用 `pywinauto` 的 `print_control_identifiers()` 打印控件树辅助定位；mmui 控件用 `class_name` 匹配。
- **坐标核对**：转账/红包点击日志会打印 `rectangle` 与点击坐标，对照屏幕核实。
- **MQTT 调试**：用 `mosquitto_pub` / MQTTX 向 `subscribe` topic 下发任务，观察 `callback_prefix` 回调。

## 3. 扩展点

### 3.1 新增 admin 指令
在 `wxbot/commands.py` `handle()` 的分发链里加分支，用 `bot_config.set(key, value)` 写回配置并持久化；更新 `_help()`。指令仅 `config.admin` 生效。

### 3.2 新增 MQTT 任务类型
1. `wxbot/mqtt/executor.py` 加 `_execute_xxx(self, task)` 方法；
2. 注册到 `TASK_METHOD_MAP`（类属性）；
3. payload 字段经 `_field(task, key)` 读取（兼容顶层/params 嵌套）；
4. 长文本/目标用 `_validate_str(value, name, MAX_*_LEN)` 校验；
5. UI 操作用 `self._enter_ui()` / `self._exit_ui()` 持锁；
6. 在 [接口文档](../API.md#42-任务-event-类型) 补 event 说明。

### 3.3 新增定时/随机任务
- 固定时刻：`scheduled_msg_list` / `scheduled_moments_list`，由 `scheduler._register_*` 注册。
- 随机窗口：`random_msg_list` / `random_moments_list`，由 `_RandomTaskRunner` 每日预抽触发时刻。
- 新类型需在 `scheduler` 加 `_send_xxx` 并接入 `tick`。

### 3.4 新增配置项
1. `wxbot/config.py` `DEFAULTS` 加默认值；
2. 数值型如需范围校验，在 `_normalize` 处理；
3. GUI 如需可视化，在 `wxbot_gui.py` 对应 tab 用 `_bool/_entry/_int/_list_edit/_json_edit` 注册变量（嵌套字段用 `minio_*/wk_*` 等前缀，并在 `_save_config` 跳过通用循环、单独组装）。

## 4. 代码约定

- **线程安全**：跨线程共享状态一律封装方法 + 锁，禁止外部 `obj._dict[k]=v` 直写（见 [经验教训 10.7](../MANUAL.md#107-跨线程共享状态要封装加锁)）。
- **UI 操作持锁**：任何微信 UI 操作经 `_enter_ui` / `_exit_ui`（或 `_ui_lock` 上下文），`_exit` 内用锁身份比较防误清。
- **剪贴板**：只用 `WinSettings._with_clipboard`，不要直接 `win32clipboard.OpenClipboard`。
- **异常兜底**：后台线程目标函数用 `try/except` 包住循环体；主循环不可因单轮异常退出。
- **日志脱敏**：避免明文打印密码/secret_key；wxid/微信号属账号标识，外发日志需评估。
- **中文**：所有面向用户文本、注释用中文；代码标识符用英文。

## 5. 打包

```powershell
pip install opencv-python pyinstaller
pyinstaller wxbot_gui.spec   # 产物 dist/wxbot.exe + config/ 模板图
```

模板图（`config/images/*.png`）缺失则对应功能静默降级。详见 [操作手册·打包](../MANUAL.md#24-打包为-exe)。

## 6. 相关阅读

- [架构设计](./Architecture.md) — 理解线程模型与数据流
- [操作手册·第 10 章 经验教训](../MANUAL.md#10-经验教训与踩坑记录) — **开发前必读**，避免重复踩坑
