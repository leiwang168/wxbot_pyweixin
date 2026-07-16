# 项目概述

## 一句话定位

wxbot_pyweixin 是一个**运行在 Windows 微信 4.1.x 客户端上的可配置自动化机器人**，通过 `pywinauto` 驱动微信 UI，实现消息收发、数字员工（AI 应答）、定时任务、朋友圈、新好友、转账红包自动处理，并可经 MQTT 与 OpenClaw agent 对接实现一对多远程控制。

## 解决什么问题

- **不依赖付费 wxautox4**：复刻 SiverWXbot_plus 核心配置能力，底层换用开源 pyweixin（pywechat）SDK。
- **开箱即用的数字员工**：AI 大脑 + 岗位人设 + FAQ 知识库 + 客户档案 CRM + 转人工，构成完整业务决策链。
- **远程可控**：MQTT 通道让一个 OpenClaw（龙虾）agent 同时控制多个微信机器人客户端。
- **运行稳健**：主循环多层兜底、UI 锁与重建机制、剪贴板串行化、消息去重，长期运行不易静默崩溃。

## 核心能力矩阵

| 领域 | 能力 |
|------|------|
| **消息** | 全局/白名单监听、关键词回复、自定义转发、只监听、随机延时、超长分段、对话记忆 |
| **数字员工** | OpenAI 兼容 AI（DeepSeek/通义/智谱等）、岗位人设、FAQ 知识库（精确+模糊）、客户档案 CRM、转人工 |
| **定时** | 固定时刻消息/朋友圈、随机窗口任务（每日/每周 N 天/每月 N 天） |
| **朋友圈** | 图文发布、随机点赞活跃账号 |
| **新好友** | 自动通过申请、改备注（前缀+昵称+后缀+时间戳）、打招呼、主动加好友（限流+配额） |
| **转账/红包** | 收到转账自动收款、红包自动拆开、飞书提醒 |
| **被删/拉黑检测** | 发消息后检测对方状态，飞书告警 + MQTT 系统事件回执 |
| **MQTT 通道** | 任务下send_text/add_friend/发朋友圈/拉取聊天记录/导出好友朋友圈等 9 类任务，回调结果 |
| **运维** | 人工操作屏蔽（鼠标钩子）、每日启停、主循环异常自恢复、日志实时查看（GUI） |

## 技术栈

| 层 | 技术 |
|----|------|
| 语言 | Python 3.10+（Windows 10/11 64-bit） |
| UI 自动化 | `pywinauto`（UIA backend）、`pyautogui`、`pywin32`（win32clipboard 等） |
| 图像识别 | `opencv-python` + `PIL.ImageGrab`（转账/红包按钮模板匹配、弹窗清理） |
| AI | OpenAI 兼容 SDK（`openai`） |
| 通信 | `paho-mqtt`（MQTT 数字员工通道）、`requests`（Webhook） |
| GUI | `tkinter` + `ttk`（配置界面、服务监控、实时日志） |
| 对象存储 | `minio`（富媒体消息上传，可选） |
| 调度 | `schedule`（固定时刻）+ 自研随机窗口运行器 |

## 与上下游项目的关系

- **[pywechat / pyweixin](https://github.com/Hello-Mr-Crab/pywechat)**：底层 SDK 源，本项目内嵌为本地包 `pyweixin/`。
- **[SiverWXbot_plus](https://github.com/SilverKing/SiverWXbot_plus)**：配置与业务逻辑的参照原型（原基于 wxautox4，本项目替换为 pyweixin）。
- **OpenClaw（龙虾）agent**：上游控制端，通过 MQTT 下发任务、接收回调与消息转发；`openclaw/` 目录含示例工具。

## 运行形态

- **CLI**：`python main.py`（前台主循环 + 后台调度线程）
- **GUI**：`python wxbot_gui.py`（tkinter 配置 + 启停 + 状态 + 日志）
- **打包**：`pyinstaller wxbot_gui.spec` → 单文件 `wxbot.exe`（含 Splash）

> 详细安装/运行/打包见 [操作手册](../MANUAL.md#2-安装与运行) 与 [README](../../README.md)。
