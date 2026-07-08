# -*- coding: utf-8 -*-
"""测试朋友圈大图/视频封面保存（端到端，走 dump_friend_moments_range + MinIO）。

改下面的 FRIEND / START / END，跑： python -u test_moment_media.py
会真实打开好友朋友圈、遍历、有图点开大图保存原图、有视频点开预览截图、上传 MinIO。
打印每条的 imageUrls / videoCoverUrl / screenshotUrl。
"""
import os
import sys

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from wxbot.config import bot_config  # noqa: E402
from wxbot.mqtt.common import MinioUploader  # noqa: E402
from wxbot.moments_export import dump_friend_moments_range  # noqa: E402

# ===== 测试参数（按需修改）=====
FRIEND = '周丽'        # 改成有图/视频朋友圈的好友
START = '2026-07-01'
END = '2026-07-06'
LIMIT = 5                # 最多取几条（每条有图/视频要点开，耗时，别太大）
# ================================


def main():
    bot_config.load()  # 显式加载 config.json（单例 import 时只装默认值，不读文件）
    cfg = bot_config.get("mqtt_worker", {}).get("minio", {}) or {}
    uploader = MinioUploader(cfg)
    print(f'>>> dump [{FRIEND}] 朋友圈 [{START}~{END}] limit={LIMIT} '
          f'minio={getattr(uploader, "available", False)}\n')

    posts = dump_friend_moments_range(
        friend=FRIEND, start=START, end=END, uploader=uploader, limit=LIMIT)

    if posts is None:
        print('!! 获取朋友圈内容异常（返回 None，未取到任何条目）')
        return
    if not posts:
        print('!! 该范围内没有朋友圈内容（空列表）')
        return

    print(f'\n==== 共 {len(posts)} 条 ====')
    for i, p in enumerate(posts):
        print(f"\n[{i}] {p.get('发布时间')} | 图{p.get('图片数量')} 视频{p.get('视频数量')}")
        print(f"    内容: {p.get('内容', '')[:200]!r}")
        print(f"    imageUrls   = {p.get('imageUrls', [])}")
        print(f"    videoCoverUrl= {p.get('videoCoverUrl', '')}")
        print(f"    screenshotUrl= {p.get('screenshotUrl', '')}")


if __name__ == '__main__':
    main()
