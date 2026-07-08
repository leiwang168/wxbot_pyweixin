# -*- coding: utf-8 -*-
"""单元测试：get_contacts_cache 排除 pending 逻辑（不依赖微信 UI）。"""
import sys
import json
import tempfile
import os

sys.stdout.reconfigure(encoding='utf-8')

from wxbot.mqtt.resolver import ContactResolver
from wxbot.mqtt.executor import TaskExecutor

# 构造临时缓存：3 个联系人，其中"张三"在 pending
cache_friends = [
    {"昵称": "张三昵称", "微信号": "zs123", "备注": "张三", "地区": "北京"},
    {"昵称": "李四", "微信号": "ls456", "备注": "", "地区": "上海"},
    {"昵称": "王五", "微信号": "ww789", "备注": "老王", "地区": "广州"},
]
pending = [{"match": "张三", "added_at": __import__("time").time()}, {"match": "李四", "added_at": __import__("time").time()}]

with tempfile.TemporaryDirectory() as td:
    cache_path = os.path.join(td, "contacts_cache.json")
    pending_path = os.path.join(td, "pending_friends.json")
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump({"updated_at": 1780000000, "count": 3, "friends": cache_friends}, f, ensure_ascii=False)
    with open(pending_path, "w", encoding="utf-8") as f:
        json.dump(pending, f, ensure_ascii=False)

    # 用临时路径构造 resolver（绕过启动自动刷新）
    import wxbot.mqtt.resolver as rmod
    r_orig = rmod._CACHE_PATH
    rmod._CACHE_PATH = cache_path
    # pending_friends 路径
    import wxbot.pending_friends as pmod
    p_orig = pmod._PENDING_PATH
    pmod._PENDING_PATH = pending_path

    try:
        resolver = ContactResolver(log_func=lambda *a: None)
        print(f"get_all_contacts: {len(resolver.get_all_contacts())} 条")

        # 单独验证 load_pending
        from wxbot.pending_friends import load_pending
        lp = load_pending()
        print(f"load_pending: {lp}")
        print(f"pending_matches: {{p.get('match','').strip() for p in lp}}")

        # 直接调 executor 方法（绕过 resolver 初始化的微信依赖）
        class FakeExec:
            pass
        fe = FakeExec()
        fe.resolver = resolver
        result = TaskExecutor._execute_get_contacts_cache(fe, {"correlationId": "test"})
        print(f"\n结果: count={result['count']} excluded_pending={result['excluded_pending']}")
        print("保留的联系人:")
        for c in result["contacts"]:
            print(f"  - 备注={c.get('备注')!r} 昵称={c.get('昵称')!r} 微信号={c.get('微信号')}")

        # 断言
        remarks = {c.get("备注", "") for c in result["contacts"]}
        nicks = {c.get("昵称", "") for c in result["contacts"]}
        assert "张三" not in remarks, "张三(备注在pending)应被排除"
        assert "李四" not in nicks, "李四(昵称在pending)应被排除"
        assert "老王" in remarks, "老王应保留"
        assert result["count"] == 1, f"应保留1条,实际{result['count']}"
        assert result["excluded_pending"] == 2, f"应排除2条,实际{result['excluded_pending']}"
        print("\n✅ 全部断言通过")
    finally:
        rmod._CACHE_PATH = r_orig
        pmod._PENDING_PATH = p_orig
