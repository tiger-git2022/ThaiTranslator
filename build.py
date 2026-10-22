"""打包成 exe：python build.py → dist\\泰语字幕翻译\\（整个文件夹拷到其他电脑即可用）。
python build.py --onefile → dist\\泰语字幕翻译-<版本>-单文件.exe（只有一个 exe，启动时先解压到临时目录，稍慢）。"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "泰语字幕翻译"
ICON = ROOT / "assets" / "app.ico"


def make_icon():
    if not ICON.exists():
        raise SystemExit(f"缺少图标 {ICON}")


def prune(qt):
    """删掉用不到的 Qt 组件（软件 OpenGL、PDF、QML、非中文翻译），约省 50MB。"""
    for name in ("opengl32sw.dll", "Qt6Pdf.dll", "Qt6Quick.dll", "Qt6Qml.dll", "Qt6QmlModels.dll",
                 "Qt6QmlMeta.dll", "Qt6QmlWorkerScript.dll", "plugins/imageformats/qpdf.dll"):
        (qt / name).unlink(missing_ok=True)
    for f in (qt / "translations").glob("*.qm"):
        if "zh_CN" not in f.name:
            f.unlink()


def make_uia():
    """预先生成 comtypes 的 UI Automation 接口模块（Teams 桌面版字幕用），打包进 exe，运行时不用再生成。"""
    import comtypes.client
    comtypes.client.GetModule("UIAutomationCore.dll")


PRUNE = ("opengl32sw.dll", "Qt6Pdf.dll", "Qt6Quick.dll", "Qt6Qml.dll", "Qt6QmlModels.dll", "Qt6QmlMeta.dll",
         "Qt6QmlWorkerScript.dll", "qpdf.dll")

ONEFILE_SPEC = """
import os
a = Analysis([{main!r}], pathex=[{app!r}], hiddenimports=['comtypes.gen.UIAutomationClient'],
             datas=[({web!r}, 'web'), ({models!r}, 'models'), ({png!r}, 'assets'), ({ext!r}, 'extension')])
# 和文件夹版 prune() 一样去掉用不到的 Qt 组件，exe 小一些、解压快一些
def keep(entry):
    name = entry[0].replace(os.sep, '/')
    base = name.rsplit('/', 1)[-1]
    if base in {prune!r}:
        return False
    return not ('PySide6/translations/' in name and name.endswith('.qm') and 'zh_CN' not in base)
a.binaries = [e for e in a.binaries if keep(e)]
a.datas = [e for e in a.datas if keep(e)]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name={name!r}, icon={icon!r}, console=False, upx=False)
"""


def version():
    return re.search(r'APP_VERSION = "([^"]+)"', (ROOT / "app" / "main.py").read_text(encoding="utf-8")).group(1)


def onefile():
    """单文件版：输出到 dist\\泰语字幕翻译-<版本>-单文件.exe，不动文件夹版和已有 zip。"""
    make_icon()
    make_uia()
    name = f"{NAME}-{version()}-单文件"
    work = ROOT / "build" / "onefile"
    work.mkdir(parents=True, exist_ok=True)
    spec = work / f"{name}.spec"
    spec.write_text(ONEFILE_SPEC.format(
        main=str(ROOT / "app" / "main.py"), app=str(ROOT / "app"), web=str(ROOT / "app" / "web"),
        models=str(ROOT / "app" / "models"), png=str(ROOT / "assets" / "app.png"), ext=str(ROOT / "extension"),
        prune=PRUNE, name=name, icon=str(ICON)), encoding="utf-8")
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--distpath", str(ROOT / "dist"),
                    "--workpath", str(work), str(spec)], check=True)
    print(f"\n完成：{ROOT / 'dist' / (name + '.exe')}")


def mac():
    """Mac 版：dist/泰语字幕翻译.app（只有浏览器扩展字幕 + 翻译 + 浮窗 + 共享，见 app/plat.py）。
    没有苹果开发者签名，只做免费的本机签名（ad-hoc），第一次打开要在“隐私与安全性”里点“仍要打开”。"""
    import plistlib
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--windowed", "--name", NAME,
                    "--icon", str(ROOT / "assets" / "app.png"), "--osx-bundle-identifier", "io.github.tiger-git2022.thaitranslator",
                    "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build"),
                    "--specpath", str(ROOT / "build"), "--paths", str(ROOT / "app"),
                    "--exclude-module", "comtypes", "--exclude-module", "pyaudiowpatch",
                    "--exclude-module", "onnxruntime", "--exclude-module", "scipy",
                    "--add-data", f"{ROOT / 'app' / 'web'}:web",
                    "--add-data", f"{ROOT / 'app' / 'models' / 'english_words.txt'}:models",
                    "--add-data", f"{ROOT / 'assets' / 'app.png'}:assets",
                    "--add-data", f"{ROOT / 'extension'}:extension",
                    str(ROOT / "app" / "main.py")], check=True)
    out = ROOT / "dist" / f"{NAME}.app"
    plist = out / "Contents" / "Info.plist"
    info = plistlib.loads(plist.read_bytes())
    info.update({
        "CFBundleShortVersionString": version(), "CFBundleVersion": version(),
        "CFBundleDisplayName": NAME, "LSMinimumSystemVersion": "12.0", "NSHighResolutionCapable": True,
        # 局域网共享、查找共享主机要用本地网络（macOS 15 起会弹窗询问）
        "NSLocalNetworkUsageDescription": "用于在局域网里共享字幕、查找正在共享的电脑。",
    })
    plist.write_bytes(plistlib.dumps(info))
    # 改了 Info.plist 要重新签名，否则 Apple 芯片的 Mac 会提示“已损坏”
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(out)], check=True)
    print(f"\n完成：{out}")


def main():
    if sys.platform == "darwin":
        return mac()
    if "--onefile" in sys.argv:
        return onefile()
    make_icon()
    make_uia()
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--windowed", "--name", NAME,
                    "--icon", str(ICON), "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build"),
                    "--specpath", str(ROOT / "build"), "--paths", str(ROOT / "app"),
                    "--hidden-import", "comtypes.gen.UIAutomationClient",
                    "--add-data", f"{ROOT / 'app' / 'web'};web",
                    "--add-data", f"{ROOT / 'app' / 'models'};models",
                    "--add-data", f"{ROOT / 'assets' / 'app.png'};assets",
                    str(ROOT / "app" / "main.py")], check=True)
    out = ROOT / "dist" / NAME
    prune(out / "_internal" / "PySide6")
    shutil.copytree(ROOT / "extension", out / "extension", dirs_exist_ok=True)
    shutil.copy2(ROOT / "README.md", out / "使用说明.md")
    print(f"\n完成：{out}")


if __name__ == "__main__":
    main()
