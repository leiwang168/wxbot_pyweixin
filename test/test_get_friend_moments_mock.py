# -*- coding: utf-8 -*-
"""离线模拟 get_friend_moments 指令：mock UI 与 MinIO，验证 executor→回调包装全链路。
不打开微信、不连真实 MinIO，只测数据流（参数校验、handler 调用、回调结构、event 改名）。
"""
import json
import sys
import time
from unittest.mock import patch

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

from wxbot.mqtt.executor import TaskExecutor


class FakeUploader:
    available = True

    def upload_named(self, local_path, object_name):
        return f"https://minio.example.com/wbot/{object_name}"


class _Resolve:
    def __init__(self, ok=True, name="浊浪精酿全国业务"):
        self.success = ok
        self.display_name = name
        self.matched_by = "remark"
        self.wxid = "wxid_zlz"
        self.error = "" if ok else "未找到匹配联系人"
        self.candidates = [] if ok else ["候选A", "候选B"]


class FakeResolver:
    def __init__(self, ok=True):
        self._ok = ok

    def resolve(self, target):
        return _Resolve(self._ok)


def fake_dump(friend, start, end, uploader, limit=50, log_func=None):
    """模拟 dump_friend_moments_range：打印入参，返回两条假朋友圈（含同日多条）。"""
    print(f"  [MOCK] dump_friend_moments_range(friend={friend!r}, start={start!r}, "
          f"end={end!r}, limit={limit}, uploader={type(uploader).__name__})")
    return [
        {"发布时间": "10:19", "发布日期": "2026-06-29", "内容": "浑浊IPA今天进罐",
         "图片数量": 1, "视频数量": 0,
         "screenshotUrl": uploader.upload_named("x", f"moment-files/{friend}/2026-06-29.png")},
        {"发布时间": "08:30", "发布日期": "2026-06-29", "内容": "周一循环：洗罐→进料",
         "图片数量": 0, "视频数量": 0,
         "screenshotUrl": uploader.upload_named("x", f"moment-files/{friend}/2026-06-29_0830.png")},
    ]


def wrap_callback(exec_result, task):
    """复刻 coordinator._process_task 的回调包装（event 改名逻辑同真机）。"""
    r = dict(exec_result)
    r["event"] = "moments_task_result" if task.get("event") == "get_friend_moments" else "task_result"
    r["executedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    r["agentId"] = "agent_001"
    r["role"] = "default"
    r["senderId"] = "wxid_self"
    r["text"] = json.dumps(r.get("result", {}), ensure_ascii=False)
    if task.get("targetName"):
        r["targetName"] = task["targetName"]
    return r


def make_executor(uploader_ok=True, resolve_ok=True):
    ex = TaskExecutor(uploader=FakeUploader() if uploader_ok else None,
                      resolver=FakeResolver(resolve_ok))
    ex._enter_ui = lambda: None   # 不碰真实 UI 锁/微信窗口
    ex._exit_ui = lambda: None
    return ex


def run(task, uploader_ok=True, resolve_ok=True):
    ex = make_executor(uploader_ok, resolve_ok)
    with patch("wxbot.moments_export.dump_friend_moments_range", fake_dump):
        return ex.execute_task(task)


def show(title, obj):
    print(f"\n{title}\n{json.dumps(obj, ensure_ascii=False, indent=2)}")


print("=" * 70 + "\n场景1 正常路径（mock UI+MinIO，返回 2 条，含同日多条 _时分 命名）\n" + "=" * 70)
task1 = {"event": "get_friend_moments", "correlationId": "mock-001",
         "targetName": "浊浪精酿全国业务", "startDate": "2026-06-20", "endDate": "2026-06-29", "limit": 50}
res1 = run(task1)
show("executor 返回:", res1)
show("模拟回调 payload (event 应为 moments_task_result):", wrap_callback(res1, task1))

print("\n" + "=" * 70 + "\n场景2 缺 target\n" + "=" * 70)
show("结果:", run({"event": "get_friend_moments", "correlationId": "m2",
                  "startDate": "2026-06-20", "endDate": "2026-06-29"}))

print("\n" + "=" * 70 + "\n场景3 缺 endDate\n" + "=" * 70)
show("结果:", run({"event": "get_friend_moments", "correlationId": "m3",
                  "targetName": "浊浪精酿全国业务", "startDate": "2026-06-20"}))

print("\n" + "=" * 70 + "\n场景4 MinIO 未配置\n" + "=" * 70)
show("结果:", run({"event": "get_friend_moments", "correlationId": "m4",
                  "targetName": "浊浪精酿全国业务", "startDate": "2026-06-20", "endDate": "2026-06-29"},
                 uploader_ok=False))

print("\n" + "=" * 70 + "\n场景5 好友未找到（resolve 失败）\n" + "=" * 70)
show("结果:", run({"event": "get_friend_moments", "correlationId": "m5",
                  "targetName": "不存在的人", "startDate": "2026-06-20", "endDate": "2026-06-29"},
                 resolve_ok=False))

print("\n" + "=" * 70 + "\n场景6 event 改名仅影响本指令（send_text 回调仍 task_result）\n" + "=" * 70)
for ev in ("get_friend_moments", "send_text", "post_moments"):
    name = "moments_task_result" if ev == "get_friend_moments" else "task_result"
    print(f"  event={ev:18s} -> 回调 event = {name}")
