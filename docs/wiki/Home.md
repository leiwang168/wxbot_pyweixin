# wxbot_pyweixin Wiki

> 基于 [pyweixin](https://github.com/Hello-Mr-Crab/pywechat) SDK（`pywinauto` UI 自动化）的可配置微信机器人，复刻 SiverWXbot_plus 的核心配置能力，**不依赖付费的 wxautox4**；支持数字员工（AI 大脑）、MQTT 远程控制（OpenClaw 通道）、朋友圈/新好友/定时任务/转账红包自动处理等。

本 Wiki 是项目知识库的**导航门户与专题补充**。权威接口与操作细节见独立文档，Wiki 不重复，只链接与提炼。

---

## 📚 文档导航

### 🚀 入门
| 文档 | 内容 |
|------|------|
| [项目概述](./Overview.md) | 是什么 / 解决什么 / 核心能力矩阵 / 技术栈 |
| [快速开始](../../README.md) | 安装、运行、首次配置、打包 exe |
| [操作手册](../MANUAL.md) | 配置详解、admin 指令、MQTT/飞书接入、运维排障、FAQ、**经验教训** |

### 🏗️ 架构
| 文档 | 内容 |
|------|------|
| [架构设计](./Architecture.md) | 分层架构、线程模型、关键数据流、并发与锁 |
| [模块清单](./Modules.md) | 各目录与模块职责、依赖关系 |

### 🔌 接口
| 文档 | 内容 |
|------|------|
| [接口文档](../API.md) | pyweixin/wxbot API、MQTT 任务 event、Webhook、配置参考 |

### 🛠️ 开发
| 文档 | 内容 |
|------|------|
| [开发指南](./Development.md) | 开发环境、调试技巧、扩展点（指令/任务/定时）、代码约定 |

### 📜 历史
| 文档 | 内容 |
|------|------|
| [变更日志](./Changelog.md) | 近期重大改动与版本节点 |

---

## 🔍 按角色速查

| 我是… | 推荐阅读路径 |
|--------|--------------|
| **部署运维** | [概述](./Overview.md) → [操作手册](../MANUAL.md) |
| **上游对接方** | [接口文档·MQTT](../API.md#4-mqtt-任务接口) → [操作手册·MQTT 接入](../MANUAL.md#5-mqtt-数字员工接入) |
| **二次开发者** | [架构](./Architecture.md) → [模块](./Modules.md) → [开发指南](./Development.md) → [经验教训](../MANUAL.md#10-经验教训与踩坑记录) |
| **故障排查** | [操作手册·FAQ](../MANUAL.md#8-常见问题) + [经验教训](../MANUAL.md#10-经验教训与踩坑记录) |

---

## 🧭 目录速览

```
wxbot_pyweixin/
├── main.py / wxbot_gui.py   入口（CLI / GUI）
├── pyweixin/                底层 SDK（UI 自动化）
├── wxbot/                   业务层（monitor/mqtt/scheduler/...）
├── config/                  运行时配置（config.json/webhook.json/persona/...）
├── docs/                    本 Wiki + API.md + MANUAL.md
├── logs/                    运行日志
└── openclaw/                OpenClaw agent 示例
```

## ⚠️ 特别注意

👎👎 请勿将本项目用于任何非法商业活动，因此造成的一切后果由使用者自行承担。
