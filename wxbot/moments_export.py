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


def _force_close_sns_window() -> int:
    """pywinauto close() 在 COM 异常下会失败,改用 win32 按 class_name/title 强关朋友圈窗口。

    win32 不经 UIA COM,COM 异常下仍可关闭。匹配 class 含 SNS 或标题为 朋友圈/Moments。
    始终打印扫描到的 mmui:: 顶层窗口,便于定位朋友圈窗口真实标识(诊断用)。
    """
    import win32gui
    import win32con
    targets = []
    mmui_tops = []

    def _cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            cls = win32gui.GetClassName(hwnd) or ''
            title = win32gui.GetWindowText(hwnd) or ''
            if cls.startswith('mmui::'):
                mmui_tops.append(cls)
            if ('SNS' in cls) or (title in ('朋友圈', 'Moments')):
                targets.append(hwnd)
        except Exception:
            pass

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception as e:
        emit("WARNING", f"[Moments] EnumWindows 异常: {e}")
    for hwnd in targets:
        try:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        except Exception:
            pass
    emit("INFO", f"[Moments] win32 扫描 mmui顶层={mmui_tops} 命中SNS={len(targets)}")
    return len(targets)


def _save_moment_images(sns_detail_list, moments_window, photo_num, tmp_dir, log):
    """点开朋友圈大图预览，逐张右键复制→剪贴板→落地原图。返回本地路径列表。
    复用 SDK dump_friend_posts.save_media 流程；任何环节异常返回已收集的(可能空)。"""
    from pyweixin.WeChatTools import mouse
    from pyweixin.Uielements import MousePos, MenuItems
    from pyweixin.WinSettings import SystemSettings
    paths = []
    try:
        comment_detail = sns_detail_list.children(control_type='ListItem', title='')[1]
        mouse.click(coords=MousePos(comment_detail).PostDetailImagePos)   # 点开大图预览
        time.sleep(0.8)
        pyautogui.press('left', presses=photo_num, interval=0.15)          # 翻到第一张
        click_pos = MousePos(sns_detail_list).PostDetailImageClickPos
        for i in range(photo_num):
            mouse.right_click(coords=click_pos)
            copy_item = moments_window.child_window(**MenuItems.CopyMenuItem)
            if not copy_item.exists(timeout=0.4):
                continue
            copy_item.click_input()
            path = os.path.join(tmp_dir, f"img_{i}.png")
            time.sleep(0.5)                                                 # 剪贴板缓存
            if SystemSettings.save_pasted_image(path):
                paths.append(path)
            pyautogui.press('right', interval=0.05)
        pyautogui.press('esc')                                              # 退出大图预览
    except Exception as e:
        log("WARNING", f"[Moments] 大图保存异常，回退截图: {e}")
        try:
            pyautogui.press('esc')
        except Exception:
            pass
    return paths


def _save_moment_video_cover(sns_detail_list, tmp_dir, log):
    """双击朋友圈视频点开预览，截图预览画面作封面。返回本地图片路径或 None。"""
    from pyweixin.WeChatTools import mouse
    from pyweixin.Uielements import MousePos, Windows
    from pywinauto import Desktop
    try:
        content_listitem = sns_detail_list.children(control_type='ListItem', title='')[0]
        click_pos = MousePos(content_listitem).PostDetailVideoClickPos
        mouse.double_click(coords=click_pos)  # 点开视频预览
        time.sleep(1.5)  # 等预览加载/首帧
        path = os.path.join(tmp_dir, "video_cover.png")
        image_preview = Desktop(backend='uia').window(**Windows.ImagePreviewWindow)
        if image_preview.exists(timeout=1.0):
            r = image_preview.rectangle()
            pyautogui.screenshot().crop((r.left, r.top, r.right, r.bottom)).save(path)
        else:
            pyautogui.screenshot().save(path)  # 预览未弹出，全屏兜底
        pyautogui.press('esc')  # 退出预览
        return path
    except Exception as e:
        log("WARNING", f"[Moments] 视频封面截图异常: {e}")
        try:
            pyautogui.press('esc')
        except Exception:
            pass
        return None


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

    posts = []
    moments_window = None
    try:
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

        recorded = 0
        used_date_keys = set()  # 同批次已用发布日期，同日多条第二条起切 _时分

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

            # 有图：优先点开大图保存原图；有视频：点开预览截图作封面；失败/都没有回退 crop
            tmp_dir = tempfile.mkdtemp(prefix="moment_imgs_")
            image_urls = []
            video_cover_url = ""
            if photo_num > 0:
                img_paths = _save_moment_images(sns_detail_list, moments_window, photo_num, tmp_dir, log)
                if img_paths and uploader and getattr(uploader, 'available', False):
                    img_stem = object_key[:-4]  # 去 .png，叠加 _{idx}.png
                    for idx, ip in enumerate(img_paths):
                        u = uploader.upload_named(ip, f"{img_stem}_{idx}.png") or ""
                        if u:
                            image_urls.append(u)
                elif img_paths:
                    log("WARNING", "[Moments] MinIO 不可用，大图未上传")
            elif video_num > 0:
                cover = _save_moment_video_cover(sns_detail_list, tmp_dir, log)
                if cover and uploader and getattr(uploader, 'available', False):
                    video_cover_url = uploader.upload_named(cover, f"{object_key[:-4]}_video.png") or ""
                elif cover:
                    log("WARNING", "[Moments] MinIO 不可用，视频封面未上传")

            # crop sns_detail_list 区域：大图/视频封面失败或纯文字时作内容截图或兜底
            tmp_path = os.path.join(tempfile.gettempdir(), f"moment_{int(time.time()*1000)}_{recorded}.png")
            url = ""
            if not image_urls and not video_cover_url:
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
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            try:
                import shutil as _shutil
                _shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass

            posts.append({
                '发布时间': post_time, '发布日期': date_str, '内容': content,
                '图片数量': photo_num, '视频数量': video_num,
                'screenshotUrl': url, 'imageUrls': image_urls,
                'videoCoverUrl': video_cover_url,
            })
            recorded += 1
            log("INFO", f"[Moments] [{recorded}] {post_time} | 图{photo_num} 视频{video_num} | {object_key}")

            if sns_detail_list.exists(timeout=0.1):
                backbutton.click_input()
            if Tools.is_sns_at_bottom(moments_list, selected[0]):
                log("INFO", "[Moments] 已到朋友圈底部")
                break
    except Exception as e:
        # 遍历异常（COM 卡死/控件缺失等）：已收集的 posts 不丢弃，返回部分结果
        log("ERROR", f"[Moments] 获取 {friend} 朋友圈异常(已收集 {len(posts)} 条): {e}")
        if not posts:
            return None  # open 阶段失败，无可返回（上层据此回调"无法查看"）
        # 已有部分：落入 finally 后 return posts（返回已收集的）
    finally:
        if moments_window is not None:
            try:
                moments_window.close()
            except Exception as ce:
                log("WARNING", f"[Moments] close() 失败(COM 异常?): {ce}")
        # 兜底:COM 异常下 close() 可能失败或静默无效,用 win32 确保朋友圈窗口关闭
        if _force_close_sns_window() > 0:
            log("INFO", "[Moments] win32 兜底关闭朋友圈窗口")

    return posts
