# -*- coding: utf-8 -*-
"""调度器：定时消息 / 定时朋友圈 / 随机窗口消息 / 随机窗口朋友圈 /
随机点赞 / 新好友 / 每日启停。

- 固定时刻任务用 `schedule` 库 + 每秒 tick。
- 随机窗口任务（random_*_list）由本模块自管的 RandomTaskRunner 负责：
  每天为匹配的任务预抽一个 [time_start, time_end] 内的随机触发时刻，
  到点执行一次并标记当日已发；跨天重置。
- 新好友与点赞为可中断等待的后台线程。

参考 SiverWXbot：send_scheduled_msg(2612) / send_scheduled_moments(2685) /
_check_random_moments(2826) / _check_random_msg(2936) / _do_moments_like(2784)。
"""
from __future__ import annotations

import random
import threading
import time
from datetime import datetime, date, timedelta
from typing import Optional

import schedule
from pyweixin import Messages

from . import friends, moments
from .config import bot_config
from .logger import log
from .reply import is_image_path, split_long_text


# ---------------------------------------------------------------------------
# 发送工具
# ---------------------------------------------------------------------------
def _split_msgs(msgs: list[str]) -> tuple[list[str], list[str]]:
    texts: list[str] = []
    images: list[str] = []
    for m in msgs or []:
        if is_image_path(m):
            images.append(m)
        elif m:
            texts.extend(split_long_text(m))
    return texts, images


def _send_msgs(targets: list[str], msgs: list[str]) -> None:
    texts, images = _split_msgs(msgs)
    for tgt in targets:
        try:
            if images:
                from pyweixin import Files
                Files.send_files_to_friend(
                    friend=tgt, files=images,
                    with_messages=bool(texts), messages=texts or [""],
                    close_weixin=False,
                )
            elif texts:
                Messages.send_messages_to_friend(friend=tgt, messages=texts, close_weixin=False)
            log.info(f"[定时消息] → {tgt}: {len(texts)}文 {len(images)}图")
            time.sleep(1)
        except Exception as e:
            log.error(f"[定时消息] → {tgt} 失败: {e}")


def _send_moments(text: str, images: list[str]) -> None:
    moments.post(text=text, images=images)


# ---------------------------------------------------------------------------
# 日期匹配
# ---------------------------------------------------------------------------
def _today_matches(task: dict, repeat_type: str) -> bool:
    today = date.today()
    if repeat_type == "daily":
        return True
    if repeat_type == "weekly":
        return today.isoweekday() in (task.get("weekdays") or [])
    if repeat_type == "monthly":
        return today.day in (task.get("dates") or [])
    return False


def _custom_today(task: dict) -> bool:
    today = date.today()
    ds = task.get("dates") or []
    iso = today.isoformat()
    return iso in ds or any(str(today) == str(d) for d in ds)


# ---------------------------------------------------------------------------
# 固定时刻任务注册
# ---------------------------------------------------------------------------
def _register_msg_tasks(sched: schedule.Scheduler) -> None:
    if not bot_config.get("scheduled_msg_switch", False):
        return
    seen: set[str] = set()
    for task in bot_config.get("scheduled_msg_list", []):
        if not task.get("enabled", True):
            continue
        t = task.get("time", "")
        if not t or t in seen:
            continue
        seen.add(t)

        def job(_task=task):
            if not _task.get("enabled", True):
                return
            rt = _task.get("repeat_type", "daily")
            if rt == "custom" or rt == "once":
                if not _custom_today(_task):
                    return
            elif not _today_matches(_task, rt):
                return
            _send_msgs(_task.get("targets") or [], _task.get("msgs") or [])

        try:
            sched.every().day.at(t).do(job)
            log.info(f"[定时消息] 注册 {t}")
        except Exception as e:
            log.error(f"[定时消息] 注册 {t} 失败: {e}")


def _register_moment_tasks(sched: schedule.Scheduler) -> None:
    if not bot_config.get("scheduled_moments_switch", False):
        return
    seen: set[str] = set()
    for task in bot_config.get("scheduled_moments_list", []):
        if not task.get("enabled", True):
            continue
        t = task.get("time", "")
        if not t or t in seen:
            continue
        seen.add(t)

        def job(_task=task):
            if not _task.get("enabled", True):
                return
            rt = _task.get("repeat_type", "daily")
            if rt == "custom" or rt == "once":
                if not _custom_today(_task):
                    return
            elif not _today_matches(_task, rt):
                return
            _send_moments(_task.get("text", ""), _task.get("images") or [])

        try:
            sched.every().day.at(t).do(job)
            log.info(f"[定时朋友圈] 注册 {t}")
        except Exception as e:
            log.error(f"[定时朋友圈] 注册 {t} 失败: {e}")


# ---------------------------------------------------------------------------
# 随机窗口任务运行器（random_msg_list / random_moments_list）
# ---------------------------------------------------------------------------
class _RandomTaskRunner:
    """每日为每个启用的随机任务预抽一个触发时刻，到点执行。"""

    def __init__(self) -> None:
        self._fires: dict[str, datetime] = {}   # task_id -> 今日触发时刻
        self._planned_for: Optional[date] = None
        self._lock = threading.Lock()

    @staticmethod
    def _parse_hhmm(s: str) -> Optional[datetime]:
        try:
            return datetime.strptime(s.strip(), "%H:%M")
        except Exception:
            return None

    def _plan_today(self, today: date) -> None:
        self._fires.clear()
        # 随机消息
        if bot_config.get("random_msg_switch", False):
            for task in bot_config.get("random_msg_list", []):
                self._plan_one(task, today, kind="msg")
        # 随机朋友圈
        if bot_config.get("random_moments_switch", False):
            for task in bot_config.get("random_moments_list", []):
                self._plan_one(task, today, kind="moment")
        self._planned_for = today

    def _plan_one(self, task: dict, today: date, kind: str) -> None:
        if not task.get("enabled", True):
            return
        tid = task.get("id") or f"{kind}_{id(task)}"
        rt = task.get("repeat_type", "daily")
        # 周期命中判定
        if rt == "weekly":
            # 随机抽取 random_days_count 个工作日
            n = int(task.get("random_days_count") or 1)
            chosen = self._pick_random_weekdays(n)
            if today.isoweekday() not in chosen:
                return
        elif rt == "monthly":
            n = int(task.get("random_days_count") or 1)
            chosen = self._pick_random_monthdays(today, n)
            if today.day not in chosen:
                return
        # daily 默认每天都抽
        start = self._parse_hhmm(task.get("time_start", "09:00")) or datetime.strptime("09:00", "%H:%M")
        end = self._parse_hhmm(task.get("time_end", "21:00")) or datetime.strptime("21:00", "%H:%M")
        if end < start:
            start, end = end, start
        # 在 [start, end] 之间随机一个今日时刻
        span_seconds = int((end - start).total_seconds())
        rand_offset = random.randint(0, max(span_seconds, 0))
        fire_dt = datetime.combine(today, (start + timedelta(seconds=rand_offset)).time())
        with self._lock:
            self._fires[tid] = fire_dt
        log.info(f"[随机{kind}] 计划今日 {fire_dt:%H:%M} 触发 (id={tid})")

    @staticmethod
    def _pick_random_weekdays(n: int) -> set[int]:
        n = max(1, min(n, 7))
        return set(random.sample(range(1, 8), n))

    @staticmethod
    def _pick_random_monthdays(today: date, n: int) -> set[int]:
        import calendar
        last = calendar.monthrange(today.year, today.month)[1]
        n = max(1, min(n, last))
        return set(random.sample(range(1, last + 1), n))

    def tick(self) -> None:
        today = date.today()
        if self._planned_for != today:
            self._plan_today(today)
        now = datetime.now()
        fired: list[str] = []
        with self._lock:
            items = list(self._fires.items())
        for tid, fire_dt in items:
            if now >= fire_dt:
                fired.append(tid)
        for tid in fired:
            with self._lock:
                self._fires.pop(tid, None)
            self._execute(tid)

    def _execute(self, tid: str) -> None:
        # 找回任务定义
        for kind, key, sender in (
            ("msg", "random_msg_list", lambda t: _send_msgs(t.get("targets") or [], t.get("msgs") or [])),
            ("moment", "random_moments_list", lambda t: _send_moments(t.get("text", ""), t.get("images") or [])),
        ):
            for task in bot_config.get(key, []) or []:
                if (task.get("id") or f"{kind}_{id(task)}") == tid:
                    try:
                        sender(task)
                        log.info(f"[随机任务] 已执行 {tid}")
                    except Exception as e:
                        log.error(f"[随机任务] 执行 {tid} 失败: {e}")
                    return


# ---------------------------------------------------------------------------
# 启停
# ---------------------------------------------------------------------------
class Scheduler:
    def __init__(self) -> None:
        self._sched = schedule.Scheduler()
        self._random = _RandomTaskRunner()
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()

    def start(self) -> None:
        _register_msg_tasks(self._sched)
        _register_moment_tasks(self._sched)

        def tick():
            while not self._stop.is_set():
                try:
                    self._sched.run_pending()
                    self._random.tick()
                except Exception as e:
                    log.error(f"[调度tick] {e}")
                self._stop.wait(5)

        t1 = threading.Thread(target=friends.new_friend_loop, args=(self._stop,), name="new_friend", daemon=True)
        t2 = threading.Thread(target=moments.like_loop, args=(self._stop,), name="moments_like", daemon=True)
        t3 = threading.Thread(target=tick, name="scheduler_tick", daemon=True)
        for t in (t1, t2, t3):
            t.start()
            self._threads.append(t)
        log.info("⏰ 调度器已启动（定时消息/朋友圈 + 随机窗口 + 新好友 + 点赞）")

    def stop(self) -> None:
        self._stop.set()
        log.info("⏰ 调度器停止信号已发送")


# 全局单例
scheduler = Scheduler()
