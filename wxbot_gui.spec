# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 — wxbot 微信机器人单文件 exe。"""

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

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='wxbot',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,   # 无控制台窗口（tkinter 自带 UI）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    onefile=True,
)
