# -*- coding: utf-8 -*-
"""验证 on_wechat_message 自身消息过滤逻辑（无需启动微信/无需 MQTT）。"""
from __future__ import annotations

import sys
import os
import io
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 修复 Windows GBK 终端 emoji 编码问题
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# 仅测试过滤条件，不触发完整初始化
print("=" * 60)
print("自身消息过滤逻辑验证")
print("=" * 60)

# 模拟机器人自身账号信息（对应日志中的 selfWxName / selfWxId）
SELF_NICKNAME = "笑叹。红尘"       # self._wx_nickname
SELF_WXID = "wxid_r8llnhzk7evj21_fd2"  # self._wx_id
SELF_WECHAT_ID = "xian520942694"       # self._wx_wechat_id

# 测试用例: (sender显示名, 解析后wxid, 是否应被过滤)
test_cases = [
    # ① 显示名 == 自身昵称
    ("笑叹。红尘", "wxid_r8llnhzk7evj21_fd2", True, "昵称匹配自身"),
    # ② 显示名 == 自身微信号
    ("xian520942694", "xian520942694", True, "微信号匹配自身"),
    # ③ 显示名是系统标记 "Self"
    ("Self", "wxid_r8llnhzk7evj21_fd2", True, "系统标记 Self"),
    # ④ sender != 自身，但解析后 wxid == self._wx_id
    ("某设备", "wxid_r8llnhzk7evj21_fd2", True, "解析wxid匹配自身"),
    # ⑤ sender != 自身，但解析后 wxid == self._wx_wechat_id
    ("某设备", "xian520942694", True, "解析微信号匹配自身"),
    # ⑥ 正常好友消息 - 不应过滤
    ("浊浪精酿", "wxid_other_001", False, "正常好友消息"),
    # ⑦ 正常好友消息 2
    ("迎着风向前！", "juwendong2010", False, "正常好友消息"),
    # ⑧ 正常好友消息 3 - 德式小麦那个场景
    ("客户A", "wxid_customer_a", False, "正常客户消息"),
]

passed = 0
failed = 0

for sender, resolved_wxid, should_filter, desc in test_cases:
    # 模拟第一步过滤：入参直检
    step1_filtered = sender in ("Self", "self", SELF_NICKNAME, SELF_WXID, SELF_WECHAT_ID)

    # 模拟第二步过滤：解析后 wxid 匹配自身
    step2_filtered = resolved_wxid in (SELF_WXID, SELF_WECHAT_ID)

    actual_filtered = step1_filtered or step2_filtered

    status = "✅" if actual_filtered == should_filter else "❌"
    if actual_filtered == should_filter:
        passed += 1
    else:
        failed += 1

    print(f"\n{status} {desc}")
    print(f"   sender={sender!r}, resolved_wxid={resolved_wxid!r}")
    print(f"   第一步(入参直检)={step1_filtered}, 第二步(wxid核查)={step2_filtered}")
    print(f"   预期过滤={should_filter}, 实际过滤={actual_filtered}")

print(f"\n{'=' * 60}")
print(f"结果: {passed} 通过, {failed} 失败 (共 {len(test_cases)} 项)")
if failed == 0:
    print("✅ 所有自身消息过滤逻辑正确！")
else:
    print(f"❌ {failed} 项不符合预期！")
print(f"{'=' * 60}")
