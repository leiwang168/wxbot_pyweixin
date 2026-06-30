# -*- coding: utf-8 -*-
"""按时间范围导出指定好友的朋友圈：内容截图 + 文本，截图上传 MinIO。

复用 pyweixin 的遍历骨架（Moments.dump_friend_posts）与时间解析
（Moments._parse_post_time / _parse_until），截图采用 pyautogui crop
sns_detail_list（已验证比 capture_as_image 更完整）。
供 MQTT executor 的 get_friend_moments 指令调用。

依赖 worker.py:_patch_open_friend_profile 的 monkeypatch（运行时全局已生效），
保证非全屏下点头像弹资料卡稳定；本模块不再重复 patch。
"""
from __future__ import annotations

import os
import re
import tempfile
import time
from datetime import datetime

import pyautogui

from pyweixin import GlobalConfig, Moments
from pyweixin.WeChatTools import Navigator, Tools
from pyweixin.Uielements import Buttons, Lists, Regex_Patterns

from .mqtt.common import emit

_ILLEGAL = r'[\\/*?:"<>|]'  # Windows 路径 / MinIO key 非法字符


def _clean_name(name: str) -> str:
    """清理好友名中的非法字符，用于 object key 与文本 replace。"""
    return re.sub(_ILLEGAL, '', name)


def _parse_date_boundary(s: str, end_of_day: bool = False):
    """解析 'YYYY-MM-DD' / 'YYYY年M月D日' 为 datetime。

    end_of_day=True 取当天 23:59:59（上界），否则取当天 00:00:00（下界）。
    借用 Moments._parse_until（支持多种日期格式）。
    """
    dt = Moments._parse_until(s)
    if end_of_day:
        return dt.replace(hour=23, minute=59, second=59)
    return dt.replace(hour=0, minute=0, second=0)


def dump_friend_moments_range(friend: str, start, end, uploader,
                              limit: int = 50, log_func=emit) -> list[dict]:
    """遍历好友朋友圈（按时间倒序），只保留发布时间落在 [start, end] 闭区间的条目。

    早于 start 即停止遍历；晚于 end（更新）的跳过不收。每条截图上传 MinIO，
    object key = moment-files/{好友}/{发布日期}.png；同一好友同一天多条时，
    第二条起用 moment-files/{好友}/{发布日期}_{发布时分}.png 避免覆盖。

    Args:
        friend:   好友备注/昵称（已由 resolver 解析为精确展示名）
        start:    'YYYY-MM-DD' 下界（含），None/空 表示不限下限
        end:      'YYYY-MM-DD' 上界（含），None/空 表示不限上限
        uploader: MinioUploader 实例（需 available）
        limit:    最多获取条数（安全上限，防止异常无限翻页）
        log_func: 日志函数
    Returns:
        list[dict]: [{'发布时间','发布日期','内容','图片数量','视频数量','screenshotUrl'}, ...]
    """
    log = log_func or emit
    clean_friend = _clean_name(friend)
    start_dt = _parse_date_boundary(start, end_of_day=False) if start else None
    end_dt = _parse_date_boundary(end, end_of_day=True) if end else None
    log("INFO", f"[Moments] 导出 {friend} 范围 [{start}, {end}] 下限={start_dt} 上限={end_dt} 上限条数={limit}")

    not_contents = ['mmui::AlbumBaseCell', 'mmui::AlbumTopCell']  # 置顶/相册封面不计

    def parse_post(listitem):
        video_num = 0
        photo_num = 0
        # 去掉好友名（备注/昵称都可能出现在详情文本里，两个都 replace）
        text = listitem.window_text().replace(friend, '').replace(clean_friend, '')
        post_time = Regex_Patterns.Snsdetail_Timestamp_pattern.findall(text)[-1]
        contain_video = re.compile(rf'\s视频\s{re.escape(post_time)}')
        content_pat = re.compile(rf'((\s包含\d+张图片\s)|(\s视频\s)).*{re.escape(post_time)}')
        m = Regex_Patterns.Contain_Images_pattern.search(text)
        if m:
            photo_num = int(m.group(1))
        if contain_video.search(text):
            video_num = 1
        content = content_pat.sub('', text)
        content = re.sub(r'^\s+', '', content)
        return content, photo_num, video_num, post_time

    GlobalConfig.close_weixin = False
    moments_window = Navigator.open_friend_moments(
        friend=friend, is_maximize=False, close_weixin=False, search_pages=5)
    backbutton = moments_window.child_window(**Buttons.BackButton)
    Tools.cancel_pin(moments_window)
    moments_list = moments_window.child_window(**Lists.MomentsList)
    sns_detail_list = moments_window.child_window(**Lists.SnsDetailList)
    moments_list.type_keys('{END}')
    moments_list.type_keys('{HOME}')
    time.sleep(1)

    posts = []
    recorded = 0
    used_date_keys = set()  # 同批次已用发布日期，同日多条第二条起切 _时分

    try:
        while recorded < limit:
            moments_list.type_keys('{DOWN}')
            selected = [li for li in moments_list.children(control_type='ListItem') if li.has_keyboard_focus()]
            if not selected or selected[0].class_name() in not_contents:
                continue
            selected[0].click_input()
            if not sns_detail_list.exists(timeout=0.3):
                pyautogui.press('esc')
                continue
            listitem = sns_detail_list.children(control_type='ListItem')[0]
            content, photo_num, video_num, post_time = parse_post(listitem)
            post_dt = Moments._parse_post_time(post_time)

            # 范围过滤（解析失败 datetime.min 不参与停止判断，按既有 dump_friend_posts 语义收下）
            if post_dt != datetime.min:
                if end_dt and post_dt > end_dt:
                    if sns_detail_list.exists(timeout=0.1):
                        backbutton.click_input()
                    continue  # 晚于 end（更新）的跳过
                if start_dt and post_dt < start_dt:
                    log("INFO", f"[Moments] 遇到早于范围下限的内容（{post_time}），停止遍历")
                    if sns_detail_list.exists(timeout=0.1):
                        backbutton.click_input()
                    break

            # 发布日期 + object key（同日多条第二条起加 _时分）
            date_str = post_dt.strftime('%Y-%m-%d') if post_dt != datetime.min else time.strftime('%Y-%m-%d')
            base_key = f"moment-files/{clean_friend}/{date_str}"
            if base_key in used_date_keys and post_dt != datetime.min:
                object_key = f"{base_key}_{post_dt.strftime('%H%M')}.png"
            else:
                object_key = f"{base_key}.png"
            used_date_keys.add(base_key)

            # 截图：crop sns_detail_list 区域（测试脚本验证的裁剪偏移 left-20/right-50）
            tmp_path = os.path.join(tempfile.gettempdir(), f"moment_{int(time.time()*1000)}_{recorded}.png")
            url = ""
            try:
                detail_rect = sns_detail_list.rectangle()
                full = pyautogui.screenshot()
                full.crop((detail_rect.left, detail_rect.top,
                           detail_rect.right, detail_rect.bottom)).save(tmp_path)
                if uploader and getattr(uploader, 'available', False):
                    url = uploader.upload_named(tmp_path, object_key) or ""
                else:
                    log("WARNING", "[Moments] MinIO 不可用，截图未上传")
            except Exception as e:
                log("WARNING", f"[Moments] 截图/上传失败: {e}")
            finally:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

            posts.append({
                '发布时间': post_time, '发布日期': date_str, '内容': content,
                '图片数量': photo_num, '视频数量': video_num, 'screenshotUrl': url,
            })
            recorded += 1
            log("INFO", f"[Moments] [{recorded}] {post_time} | 图{photo_num} 视频{video_num} | {object_key}")

            if sns_detail_list.exists(timeout=0.1):
                backbutton.click_input()
            if Tools.is_sns_at_bottom(moments_list, selected[0]):
                log("INFO", "[Moments] 已到朋友圈底部")
                break
    finally:
        try:
            moments_window.close()
        except Exception:
            pass  # 关闭失败不影响已获取内容

    return posts
