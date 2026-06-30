# -*- coding: utf-8 -*-
"""/指令 处理（首批精简）。

仅来自 config['admin'] 的消息生效。参考 SiverWXbot `process_command`（wxbot_core.py:3641）
的分发表写法。处理完写回 config.json 并热重载运行时状态。

首批指令：
  /状态                     — 查看运行状态
  /暂停私聊自动回复          — chat_listen_only = true
  /恢复私聊自动回复          — chat_listen_only = false
  /暂停群聊自动回复          — group_listen_only = true
  /恢复群聊自动回复          — group_listen_only = false
  /添加监听用户 名           — listen_list += 名
  /删除监听用户 名           — listen_list -= 名
  /添加群 名                — group += 名
  /删除群 名                — group -= 名
  /开关新好友 on|off         — new_friend_switch
  /立即发朋友圈 文本|图片    — 立即发布（多个字段用 | 分隔，图片为绝对路径）
  /记忆列表                 — 列出有记忆的窗口与条数
  /清空记忆 窗口名          — 清空指定窗口记忆
  /清空全部记忆             — 清空所有记忆
  /关键词 on|off 私聊|群聊   — 开关关键词回复
  /重载配置                  — 重新读取 config.json
"""
from __future__ import annotations

from typing import Optional

from . import moments
from . import persona, employee, customer
from .config import bot_config
from .input_blocker import input_blocker
from .logger import log
from .reply import reply_engine


def is_admin(sender: str) -> bool:
    admin = bot_config.get("admin", "文件传输助手")
    return sender == admin


def handle(text: str) -> Optional[str]:
    """处理 /指令，返回给 admin 的回复文本（None 表示静默）。"""
    if not text or not text.startswith("/"):
        return None
    cmd, _, arg = text[1:].strip().partition(" ")
    arg = arg.strip()
    c = cmd

    if c == "状态":
        return _status()

    if c == "暂停私聊自动回复":
        bot_config.set("chat_listen_only", True)
        return "✅ 私聊已切换为只监听，不自动回复。"
    if c == "恢复私聊自动回复":
        bot_config.set("chat_listen_only", False)
        return "✅ 私聊自动回复已恢复。"
    if c == "暂停群聊自动回复":
        bot_config.set("group_listen_only", True)
        return "✅ 群聊已切换为只监听，不自动回复。"
    if c == "恢复群聊自动回复":
        bot_config.set("group_listen_only", False)
        return "✅ 群聊自动回复已恢复。"

    if c == "添加监听用户":
        if not arg:
            return "❌ 用法：/添加监听用户 名"
        ok = bot_config.add_listen_user(arg)
        return f"✅ 已添加监听用户：{arg}" if ok else f"⚠️ 已在监听列表：{arg}"
    if c == "删除监听用户":
        if not arg:
            return "❌ 用法：/删除监听用户 名"
        ok = bot_config.remove_listen_user(arg)
        return f"✅ 已移除监听用户：{arg}" if ok else f"⚠️ 不在监听列表：{arg}"

    if c == "添加群":
        if not arg:
            return "❌ 用法：/添加群 名"
        ok = bot_config.add_group(arg)
        if ok:
            bot_config.set("group_switch", True)  # 添加群时自动开启群开关
            return f"✅ 已添加群：{arg}（群开关已开启）"
        return f"⚠️ 已在群列表：{arg}"
    if c == "删除群":
        if not arg:
            return "❌ 用法：/删除群 名"
        ok = bot_config.remove_group(arg)
        return f"✅ 已移除群：{arg}" if ok else f"⚠️ 不在群列表：{arg}"

    if c == "开关新好友":
        if arg.lower() in ("on", "开", "1", "true"):
            bot_config.set("new_friend_switch", True)
            return "✅ 新好友自动通过已开启。"
        if arg.lower() in ("off", "关", "0", "false"):
            bot_config.set("new_friend_switch", False)
            return "✅ 新好友自动通过已关闭。"
        return "❌ 用法：/开关新好友 on|off"

    if c == "立即发朋友圈":
        return _post_moments_now(arg)

    if c == "记忆列表":
        rows = reply_engine.memory.list_chats()
        if not rows:
            return "（暂无记忆记录）"
        return "🧠 记忆窗口：\n" + "\n".join(f"  {n}: {cnt} 条" for n, cnt in rows)

    if c == "清空记忆":
        if not arg:
            return "❌ 用法：/清空记忆 窗口名"
        reply_engine.memory.clear(arg)
        return f"✅ 已清空 {arg} 的记忆。"

    if c == "清空全部记忆":
        reply_engine.memory.clear(None)
        return "✅ 已清空全部记忆。"

    if c == "关键词":
        return _keyword_switch(arg)

    # ---- 数字员工 ----
    if c == "数字员工":
        return _de_switch(arg)
    if c == "岗位列表":
        names = persona.list_personas()
        return "👤 可用岗位：\n" + ("\n".join(f"  {n}" for n in names) if names else "（无，使用内置默认）")
    if c == "当前岗位":
        from .reply import reply_engine  # noqa
        # 简单回显默认 + 映射
        c2 = bot_config.cfg
        return (f"默认岗位：{c2.get('default_persona')}\n"
                f"私聊专属：{c2.get('chat_persona_map')}\n"
                f"群组专属：{c2.get('group_persona_map')}")
    if c == "设置岗位":
        # 用法：/设置岗位 私聊|群聊 对象名 岗位名
        return _set_persona(arg)
    if c == "客户列表":
        rows = customer.crm.list_all()
        if not rows:
            return "（暂无客户档案）"
        return "📋 客户档案：\n" + "\n".join(
            f"  {r.get('昵称')} | {r.get('状态')} | {r.get('消息数')}条 | 最近:{r.get('最近联系')}"
            for r in rows[-20:])
    if c == "客户档案":
        if not arg:
            return "❌ 用法：/客户档案 昵称"
        d = customer.crm.get(arg)
        if not d:
            return f"（无 {arg} 的档案）"
        recs = d.get("跟进记录") or []
        return (f"👤 {d.get('昵称')}\n状态：{d.get('状态')}\n消息数：{d.get('消息数')}\n"
                f"首次：{d.get('首次联系')}\n最近：{d.get('最近联系')}\n备注：{d.get('备注')}\n"
                f"跟进记录({len(recs)})：" + ("\n" + "\n".join(f"  {x.get('时间')}: {x.get('内容')}" for x in recs[-5:]) if recs else "无"))
    if c == "客户状态":
        # /客户状态 昵称 状态
        parts = arg.split()
        if len(parts) != 2:
            return "❌ 用法：/客户状态 昵称 新客户|跟进中|意向|已成交|已转人工"
        customer.crm.set_status(parts[0], parts[1])
        return f"✅ {parts[0]} 状态已更新为 {parts[1]}"
    if c == "客户备注":
        parts = arg.split(maxsplit=1)
        if len(parts) != 2:
            return "❌ 用法：/客户备注 昵称 备注内容"
        customer.crm.add_note(parts[0], parts[1])
        return f"✅ 已为 {parts[0]} 添加跟进记录"
    if c == "重载知识库":
        employee.employee.reload()
        return "✅ 知识库与 AI 接口已重载。"
    if c == "转人工":
        return _escalation_switch(arg)

    # ---- MQTT 数字员工 ----
    if c in ("员工状态", "员工重连"):
        from .mqtt.worker import mqtt_worker
        out = mqtt_worker.handle_admin_command(text)
        return out

    # ---- 好友添加扩展 ----
    if c == "添加好友":
        from .friend_add import friend_add_ext
        if not friend_add_ext._initialized:
            friend_add_ext.initialize()
        return friend_add_ext.handle_admin_command(text)

    # ---- 人工操作屏蔽 ----
    if c == "屏蔽微信":
        return input_blocker.enable(reason="admin 指令")
    if c == "解除屏蔽":
        return input_blocker.disable(reason="admin 指令")

    if c == "重载配置":
        bot_config.reload()
        reply_engine.reload_memory_settings()
        employee.employee.reload()
        from .mqtt.worker import mqtt_worker
        mqtt_worker.reconfigure()
        from .friend_add import friend_add_ext
        friend_add_ext.initialize()  # 重载好友添加扩展配置
        return "✅ 配置、记忆、数字员工、MQTT、好友添加扩展已重载。"

    return _help()


def _de_switch(arg: str) -> str:
    if arg.lower() in ("on", "开", "1", "true"):
        bot_config.set("digital_employee_switch", True)
        return "✅ 数字员工已开启（知识库+AI）。"
    if arg.lower() in ("off", "关", "0", "false"):
        bot_config.set("digital_employee_switch", False)
        return "✅ 数字员工已关闭（仅关键词/转发）。"
    return "❌ 用法：/数字员工 on|off"


def _set_persona(arg: str) -> str:
    parts = arg.split()
    if len(parts) < 3 or parts[0] not in ("私聊", "群聊"):
        return "❌ 用法：/设置岗位 私聊|群聊 对象名 岗位名"
    scope, target, pname = parts[0], parts[1], parts[2]
    key = "chat_persona_map" if scope == "私聊" else "group_persona_map"
    m = dict(bot_config.get(key, {}))
    m[target] = pname
    bot_config.set(key, m)
    return f"✅ {scope}【{target}】已绑定岗位【{pname}】"


def _escalation_switch(arg: str) -> str:
    if arg.lower() in ("on", "开", "1", "true"):
        bot_config.set("escalation_switch", True)
        return "✅ 转人工已开启。"
    if arg.lower() in ("off", "关", "0", "false"):
        bot_config.set("escalation_switch", False)
        return "✅ 转人工已关闭。"
    return "❌ 用法：/转人工 on|off"


def _keyword_switch(arg: str) -> str:
    """用法：/关键词 on|off 私聊|群聊"""
    parts = arg.split()
    if len(parts) != 2 or parts[0].lower() not in ("on", "off") or parts[1] not in ("私聊", "群聊"):
        return "❌ 用法：/关键词 on|off 私聊|群聊"
    on = parts[0].lower() == "on"
    key = "chat_keyword_switch" if parts[1] == "私聊" else "group_keyword_switch"
    bot_config.set(key, on)
    return f"✅ {parts[1]}关键词回复已{'开启' if on else '关闭'}。"


def _post_moments_now(arg: str) -> str:
    if not arg:
        return "❌ 用法：/立即发朋友圈 文本|图片绝对路径[|图片...]"
    parts = [p.strip() for p in arg.split("|") if p.strip()]
    text = ""
    images: list[str] = []
    for p in parts:
        from .reply import is_image_path
        if is_image_path(p):
            images.append(p)
        else:
            text = (text + " " + p).strip() if text else p
    ok = moments.post(text=text, images=images)
    return "✅ 朋友圈已发布。" if ok else "❌ 朋友圈发布失败，详见日志。"


def _status() -> str:
    c = bot_config.cfg
    return (
        f"📊 wxbot_pyweixin 状态\n"
        f"  全局监听(黑名单)模式: {c['AllListen_switch']}\n"
        f"  群开关: {c['group_switch']}（只监听: {c['group_listen_only']}）\n"
        f"  私聊只监听: {c['chat_listen_only']}\n"
        f"  监听用户({len(c['listen_list'])}): {c['listen_list']}\n"
        f"  监听群({len(c['group'])}): {c['group']}\n"
        f"  私聊关键词: {c['chat_keyword_switch']} / 群关键词: {c['group_keyword_switch']}\n"
        f"  新好友通过: {c['new_friend_switch']} / 打招呼: {c['new_friend_reply_switch']}\n"
        f"  朋友圈点赞: {c['moments_like_switch']} ({c['moments_like_min']}~{c['moments_like_max']}min)\n"
        f"  定时朋友圈: {c['scheduled_moments_switch']} / 随机朋友圈: {c['random_moments_switch']}\n"
        f"  定时消息: {c['scheduled_msg_switch']} / 随机消息: {c['random_msg_switch']}\n"
        f"  对话记忆: {c['memory_switch']} ({c['memory_max_count']}/{c['memory_context_count']})\n"
        f"  自定义转发: {c['custom_forward_switch']}\n"
        f"  数字员工: {c['digital_employee_switch']} | 默认岗位: {c['default_persona']}\n"
        f"  知识库: {c['knowledge_switch']} | 转人工: {c['escalation_switch']}\n"
        f"  AI接口数: {len(c['api_configs'])} (当前#{c['api_index']})\n"
        f"  人工屏蔽: {input_blocker.status() if input_blocker._started else '未启动'}"
    )


def _help() -> str:
    return (
        "📖 可用指令：\n"
        "— 基础 —\n"
        "/状态\n"
        "/暂停私聊自动回复 | /恢复私聊自动回复\n"
        "/暂停群聊自动回复 | /恢复群聊自动回复\n"
        "/添加监听用户 名 | /删除监听用户 名\n"
        "/添加群 名 | /删除群 名\n"
        "/开关新好友 on|off\n"
        "/关键词 on|off 私聊|群聊\n"
        "/立即发朋友圈 文本|图片绝对路径\n"
        "— 数字员工 —\n"
        "/数字员工 on|off\n"
        "/岗位列表 | /当前岗位 | /设置岗位 私聊|群聊 对象 岗位\n"
        "/客户列表 | /客户档案 昵称 | /客户状态 昵称 状态 | /客户备注 昵称 内容\n"
        "/转人工 on|off | /重载知识库\n"
        "— MQTT 通道 —\n"
        "/员工状态 | /员工重连\n"
        "— 好友添加 —\n"
        "/添加好友 微信号或wxid\n"
        "— 记忆与配置 —\n"
        "/记忆列表 | /清空记忆 窗口名 | /清空全部记忆\n"
        "/重载配置\n"
        "— 人工屏蔽 —\n"
        "/屏蔽微信 | /解除屏蔽（或 Ctrl+Alt+X）"
    )
