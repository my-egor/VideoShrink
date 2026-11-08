# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

project = Path(SPECPATH)
datas = [(str(project / "licenses" / name), "licenses") for name in
         ("README.txt", "FFmpeg-LICENSE.txt", "FFmpeg-BUILD-README.txt", "THIRD-PARTY-SOURCES.txt", "NSIS-LICENSE.txt", "PyInstaller-LICENSE.txt", "Python-LICENSE.txt", "TclTk-license.terms")
         if (project / "licenses" / name).exists()]
binaries = []
for name in ("ffmpeg.exe", "ffprobe.exe"):
    binary = project / "tools" / name
    if binary.exists():
        binaries.append((str(binary), "tools"))

a = Analysis([str(project / "run.py")], pathex=[str(project)], binaries=binaries, datas=datas,
             hiddenimports=["tkinter", "_tkinter"], hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="VideoShrink", debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False, disable_windowed_traceback=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="VideoShrink")
