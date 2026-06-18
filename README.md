# wxbot_pyweixin

基于 [pyweixin](https://github.com/Hello-Mr-Crab/pywechat) SDK（`pywinauto` UI 自动化）的可配置微信机器人，复刻 [SiverWXbot_plus](https://github.com/SiverKing/SiverWXbot_plus) 的核心配置能力，**不依赖付费的 `wxautox4`**。 


## 功能概览

## 配合openclaw agent 一起使用，可以让龙虾帮你远程操作wx机器人，支持一对多模式，一个龙虾，多个wx机器人客户端，通过MQTT通讯。

### 阶段一（三大优先功能最小闭环）
- ✅ **收发消息** — 全局/白名单监听、关键词回复、自定义转发、只监听模式、随机延时、超长分段
- ✅ **添加好友** — 自动通过好友申请、自动改备注（前缀+昵称+后缀+可选时间戳）、通过后打招呼
- ✅ **发朋友圈** — 图文发布、定时发布、随机点赞活跃账号
- ✅ **微信 `/指令` 管理**

### 阶段二（已追加）
- ✅ **对话记忆** — 按 `memory/<wxid>/<chat>` 文件化存档，AI 上下文携带、超长截断、可视化列表/清空
- ✅ **随机窗口定时消息/朋友圈** — `[time_start, time_end]` 内随机触发时刻，支持每天/每周随机 N 天/每月随机 N 天
- ✅ **机器人自身回复入记忆** — 关键词回复与 AI 回复一并存档
- ✅ **更多 `/指令`** — 关键词开关、记忆列表/清空、状态扩展

### 数字员工（业务核心，已交付）
- ✅ **AI 大脑** — OpenAI 兼容接口（DusAPI / DeepSeek / 通义 / 智谱 / OpenAI 通用），梯度重试、历史上下文、岗位 system prompt
- ✅ **岗位人设** — `config/persona/<岗位>.md`，私聊/群聊各自绑定不同岗位（客服/销售/助理…）
- ✅ **FAQ 知识库** — `config/knowledge.json`，精确包含 + 关键词重合度模糊匹配，命中优先于 AI
- ✅ **客户档案 CRM** — `customer/<wxid>/<朋友>.json`，自动建档、消息计数、状态流转（新客户→跟进中→意向→已成交/已转人工）、跟进记录
- ✅ **转人工** — 关键词命中即转人工，自动通知人工座席并标记客户状态
- ✅ **数字员工编排** — 转人工 → 知识库 → AI（带客户档案上下文）三级决策
- ✅ **管理指令** — 岗位列表/绑定、客户列表/档案/状态/备注、数字员工与转人工开关、知识库重载

## 环境要求

- Windows 10/11 64-bit
- 微信 4.1.9.35（已验证）
- 讲述人 trick 已处理（UI 树可见，见 `pywechat/Weixin4.0.md`）
- Python 3.10+

## 安装

```powershell
pip install -r requirements.txt
```

## 运行

```powershell
set PYTHONUTF8=1
python main.py
```

首次运行自动生成 `config/config.json`，按需编辑后重启或发 `/重载配置`。

## 配置说明

完整字段见 `wxbot/config.py` 的 `DEFAULTS`（50 项）。关键项：

| 字段 | 说明 |
|---|---|
| `admin` | 管理员昵称（可发 `/指令`） |
| `AllListen_switch` | `false`=白名单模式，`true`=全局(黑名单)模式 |
| `listen_list` / `group` | 白名单用户 / 群列表（`group_switch` 控制群总开关） |
| `chat_keyword_switch` / `group_keyword_switch` / `keyword_dict` | 关键词回复 |
| `new_friend_switch` / `new_friend_check_min/max` / `new_friend_msg` / `new_friend_remark_*` | 自动通过好友 |
| `scheduled_msg_list` / `scheduled_moments_list` | 固定时刻定时（once/daily/weekly/monthly/custom） |
| `random_msg_list` / `random_moments_list` | 随机窗口定时（time_start~time_end + daily/weekly/monthly + random_days_count） |
| `moments_like_switch` / `moments_like_min/max` | 随机点赞 |
| `custom_forward_list` | 自定义转发规则（type: keyword/all/sender；all_sources；forward_with_source） |
| `memory_switch` / `memory_max_count` / `memory_context_count` | 对话记忆 |
| **数字员工** | |
| `digital_employee_switch` | 数字员工总开关（关闭则仅关键词/转发，不调 AI） |
| `api_configs` / `api_index` | OpenAI 兼容接口列表与当前索引（`{sdk,key,url,model}`） |
| `default_persona` / `chat_persona_map` / `group_persona_map` | 岗位人设：全局默认 + 私聊/群聊专属绑定 |
| `knowledge_switch` / `knowledge_threshold` | FAQ 知识库开关与模糊匹配阈值 |
| `escalation_switch` / `escalation_keywords` / `escalation_target` | 转人工：开关、触发词、通知对象（空=admin） |
| `customer_crm_switch` | 客户档案 CRM 开关 |

**定时任务示例**：
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

## 微信指令（来自 admin）

```
— 基础 —
/状态
/暂停私聊自动回复 | /恢复私聊自动回复
/暂停群聊自动回复 | /恢复群聊自动回复
/添加监听用户 名 | /删除监听用户 名
/添加群 名 | /删除群 名
/开关新好友 on|off
/关键词 on|off 私聊|群聊
/立即发朋友圈 文本|图片绝对路径
— 数字员工 —
/数字员工 on|off
/岗位列表 | /当前岗位 | /设置岗位 私聊|群聊 对象 岗位
/客户列表 | /客户档案 昵称 | /客户状态 昵称 状态 | /客户备注 昵称 内容
/转人工 on|off | /重载知识库
— 记忆与配置 —
/记忆列表 | /清空记忆 窗口名 | /清空全部记忆
/重载配置
```

## 快速上手数字员工

1. 编辑 `config/config.json`，填入 AI 接口：
   ```jsonc
   "api_configs": [{"sdk":"openai","key":"sk-你的key","url":"https://api.deepseek.com/v1","model":"deepseek-chat"}]
   ```
2. 在 `config/persona/` 新建岗位文件（如 `销售.md`），写明人设。
3. 在 `config/knowledge.json` 添加业务 FAQ：`{"q":["价格","多少钱"],"a":"产品定价为…"}`。
4. `python main.py` 启动；客户发消息即由「知识库 → AI」自动应答，含"转人工"则转座席并建档。

## 目录结构

```
wxbot_pyweixin/
├── main.py              # 入口（解析 wxid、启动调度器与主循环）
├── pyweixin/            # 内嵌 SDK（源自 pywechat/src/pyweixin）
├── config/config.json   # 首次运行自动生成
├── memory/<wxid>/...    # 对话记忆（运行时生成）
├── logs/                # 运行日志
├── openclaw/            # openclaw agent 示例
├── requirements.txt
└── wxbot/
    ├── config.py        # BotConfig 配置管理
    ├── monitor.py       # 消息主循环（双轨监听）
    ├── reply.py         # 回复编排（关键词/转发/延时/记忆）
    ├── friends.py       # 新好友自动通过+备注+打招呼
    ├── moments.py       # 朋友圈发布/点赞
    ├── scheduler.py     # 固定/随机定时调度
    ├── commands.py      # /指令
    ├── memory.py        # 对话记忆（文件化）
    ├── ai_base.py       # AI 抽象基类
    ├── ai_openai.py     # OpenAI 兼容接口（数字员工大脑）
    ├── persona.py       # 岗位人设管理
    ├── knowledge.py     # FAQ 知识库
    ├── customer.py      # 客户档案 CRM
    ├── employee.py      # 数字员工编排（转人工/知识库/AI）
    └── logger.py
```

## 已知限制

1. pyweixin 无"好友打标签"接口 → `new_friend_tags` 记日志跳过。
2. `Moments.post_moments` 不支持隐私/标签 → 朋友圈隐私控制待评估扩展 SDK。
3. 群聊"引用原消息"能力待运行时验证 → `group_reply_quote` 默认 `false`。
4. 群消息的真实发送人解析待精细化（阶段二后续）。
5. `check_new_friends` 有频率限制（单次≤8/每日≤4/间隔≥2h），调度取保守间隔。

## 验证（手工，仿 pywechat/test_*.py）

前置：微信 4.1.x 已登录 + 讲述人 trick + `set PYTHONUTF8=1`。
1. **收发**：另一账号向 bot 发关键词 → 命中 `keyword_dict` 自动回复；非白名单 → 不响应；`AllListen_switch=true` → 全局。
2. **加好友**：新号发申请 → 等 `new_friend_check_min~max` → 自动通过+备注+打招呼。
3. **朋友圈**：`/立即发朋友圈 你好|C:\x.jpg` → 发布；配一条 1 分钟后 `scheduled_moments_list` → 定时发布；`moments_like_switch=true` → 随机点赞。
4. **记忆**：发几条消息后 `/记忆列表` → 查看条数；`/清空记忆 窗口名`。
5. **随机定时**：配一条 `time_start/time_end` 跨当前时刻的 `random_msg_list` → 到随机时刻自动发送。


##  特别鸣谢 
1、 [pyweixin](https://github.com/Hello-Mr-Crab/pywechat)
2、 [SiverWXbot_plus](https://github.com/SiverKing/SiverWXbot_plus)

## 特别注意
👎👎请勿将wxbot_pyweixin用于任何非法商业活动，因此造成的一切后果由使用者自行承担！