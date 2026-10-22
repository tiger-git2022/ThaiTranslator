"""把 build.py 的打包结果整理成 Release 文件，放到 out\\：
ThaiCaption-<版本>-windows-x64.zip（里面是“泰语字幕翻译”文件夹）和 ThaiCaption-<版本>-windows-x64-onefile.exe。
Release 文件名用英文，避免 GitHub 把中文文件名改掉。中文路径都在 Python 里处理，不经过命令行参数。"""
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import build  # noqa: E402

v = build.version()
dist, out = ROOT / "dist", ROOT / "out"
out.mkdir(exist_ok=True)

folder = dist / build.NAME
zip_path = out / f"ThaiCaption-{v}-windows-x64.zip"
with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for f in sorted(folder.rglob("*")):
        z.write(f, f.relative_to(dist).as_posix())   # 非 ASCII 文件名自动按 UTF-8 写入，Windows 资源管理器能正确显示

shutil.copy2(dist / f"{build.NAME}-{v}-单文件.exe", out / f"ThaiCaption-{v}-windows-x64-onefile.exe")
for f in sorted(out.iterdir()):
    print(f"{f.name}  {f.stat().st_size / 2**20:.1f} MB")
