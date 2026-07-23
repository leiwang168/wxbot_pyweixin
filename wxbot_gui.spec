# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 — wxbot 微信机器人单文件 exe。"""
import os
from PyInstaller.building.splash import Splash

# 清理上次打包残留的 pyi_splash.py：Splash 会把 Tcl 运行时脚本写到 CWD，
# 下次打包 Analysis 会误把它当 Python 源码编译（SyntaxError: invalid syntax）。
# 必须在 Analysis 之前删除。
for _residue in ('pyi_splash.py',):
    _rp = os.path.join(os.getcwd(), _residue)
    if os.path.exists(_rp):
        os.remove(_rp)

a = Analysis(
    ['wxbot_gui.py'],
    pathex=[],
    binaries=[],
    datas=[
        # 模板图（转账收款/红包拆开按钮）：打包到 _MEIPASS/config/images
        ('config/images', 'config/images'),
    ],
    hiddenimports=[
        # pywin32
        'win32gui', 'win32con', 'win32api', 'win32clipboard',
        'win32process', 'win32file', 'win32event',
        'win32com', 'win32com.client',
        # pywinauto
        'pywinauto', 'pywinauto.controls', 'pywinauto.controls.uia_controls',
        'pywinauto.backend', 'pywinauto.backend.uia_element_info',
        # pycaw / comtypes
        'pycaw', 'pycaw.pycaw',
        'comtypes', 'comtypes.client',
        # MQTT
        'paho', 'paho.mqtt', 'paho.mqtt.client',
        # 音频
        'sounddevice', 'soundfile',
        # 图像处理（模板匹配）
        'cv2', 'numpy',
        'PIL', 'PIL._tkinter_finder',
        # 其他
        'emoji',
        'packaging',
        'schedule',
        'requests',
        'minio',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter.test', 'unittest', 'xmlrpc',
        'pydoc', 'doctest',
        'matplotlib', 'scipy', 'pandas',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

# 启动 Splash：bootloader 解压 + Python import 期间显示，覆盖 onefile 启动黑屏期
splash = Splash(
    'config/images/splash.png',
    binaries=a.binaries,
    datas=a.datas,
    text_pos=(110, 268),   # 文字位置（图片坐标系，副标题下方进度条区）
    text_size=12,
    text_color='white',
    minify_script=True,
    script_name='pyi_splash.py',
)

exe = EXE(
    pyz,
    a.scripts,
    splash,
    a.binaries,
    a.datas,
    [],
    name='wxbot',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,        # 关闭 UPX：UPX 压缩的 exe 易被杀毒误报为木马
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,   # 无控制台窗口（tkinter 自带 UI）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    onefile=True,
    # version='version_info.txt',  # 可选：exe 版本元数据（PyInstaller 对该文件编码敏感，按需调试）
)
