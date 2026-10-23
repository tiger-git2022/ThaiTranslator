"""自动更新的替换脚本自检（只在 Windows 上跑，用系统自带的 Windows PowerShell 5.1）。

造一个旧版本的程序文件夹和一个新版本的 zip，让子进程调用 updater.start_install 后退出，
检查：文件被覆盖、_internal 整个替换、不在安装包里的文件保留。"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "泰语字幕翻译"

if sys.platform != "win32":
    print("跳过：只在 Windows 上测")
    sys.exit(0)

base = Path(tempfile.mkdtemp(prefix="thaicaption-update-"))
app = base / "安装目录" / NAME
(app / "_internal").mkdir(parents=True)
(app / "_internal" / "stale.txt").write_text("old")
(app / "使用说明.md").write_text("old")
(app / "我的文件.txt").write_text("keep")
# “程序”用 whoami.exe 的副本：脚本最后重新打开它时会直接运行完退出
exe_src = Path(os.environ["SystemRoot"]) / "System32" / "whoami.exe"
shutil.copy(exe_src, app / f"{NAME}.exe")

new = base / "new" / NAME
(new / "_internal").mkdir(parents=True)
(new / "_internal" / "a.txt").write_text("new")
(new / "使用说明.md").write_text("new")
shutil.copy(exe_src, new / f"{NAME}.exe")
work = base / "work"
work.mkdir()
zip_path = work / "update.zip"
with zipfile.ZipFile(zip_path, "w") as z:
    for f in new.rglob("*"):
        z.write(f, f.relative_to(new.parent))

child = f"""
import sys, pathlib
sys.path.insert(0, {str(ROOT / 'app')!r})
import updater as u
u.mode = lambda: "zip"
u.APP_DIR = pathlib.Path({str(app)!r})
u.EXE = u.APP_DIR / {NAME + '.exe'!r}
u.start_install({str(zip_path)!r})
"""
subprocess.run([sys.executable, "-c", child], check=True)
log = work / "update.log"
for _ in range(120):
    if log.exists():
        break
    time.sleep(1)
time.sleep(2)
text = log.read_text(encoding="utf-8-sig") if log.exists() else "(没有 update.log)"
print("update.log:", text)
problems = []
if not text.startswith("ok"):
    problems.append("脚本没有成功")
if (app / "_internal" / "stale.txt").exists():
    problems.append("_internal 没有整个替换")
if not (app / "_internal" / "a.txt").exists():
    problems.append("_internal 里的新文件没复制过去")
if (app / "使用说明.md").read_text() != "new":
    problems.append("文件没有被覆盖")
if not (app / "我的文件.txt").exists():
    problems.append("不在安装包里的文件被删了")
if problems:
    print("自动更新自检失败：", "；".join(problems))
    sys.exit(1)
print("自动更新自检通过")
