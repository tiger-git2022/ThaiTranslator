"""检查 GitHub Releases 上的新版本，下载、校验 SHA-256 并替换程序文件。

Windows 打包版：把新文件解压到临时文件夹，启动一个隐藏的 PowerShell 脚本后程序退出；
脚本等进程结束，把新文件覆盖进程序文件夹（文件夹整个替换），再重新打开程序。
程序文件夹里不在安装包里的文件不会动。单文件版直接换掉那个 exe。
Mac 版和源码运行不自动替换，只打开下载页面。
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

REPO = "tiger-git2022/ThaiTranslator"
RELEASES_URL = f"https://github.com/{REPO}/releases/latest"
TIMEOUT = 30

FROZEN = getattr(sys, "frozen", False)
EXE = Path(sys.executable).resolve()
APP_DIR = EXE.parent
ONEDIR = FROZEN and (APP_DIR / "_internal").is_dir()   # PyInstaller 文件夹版；单文件版没有 _internal


def mode():
    """'zip'：文件夹版；'exe'：单文件版；None：不能自动替换（Mac、源码运行）。"""
    if not FROZEN or sys.platform != "win32":
        return None
    return "zip" if ONEDIR else "exe"


def asset_name(version):
    return f"ThaiCaption-{version}-windows-x64.zip" if mode() == "zip" else f"ThaiCaption-{version}-windows-x64-onefile.exe"


def parse(v):
    m = re.match(r"v?(\d+)\.(\d+)(?:\.(\d+))?", (v or "").strip())
    return tuple(int(x or 0) for x in m.groups()) if m else None


MANIFEST_URL = f"https://github.com/{REPO}/releases/latest/download/update.json"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"


def _get(url, accept="application/octet-stream"):
    req = urllib.request.Request(url, headers={"User-Agent": "ThaiCaption-updater", "Accept": accept})
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def check(current):
    """有比 current 新的版本就返回 dict(version, notes, page, url, sha256)，否则返回 None。网络出错时抛异常。

    先读 Release 里的 update.json（和下载安装包一样没有次数限制）。v1.5.10 及以前的 Release 没有这个文件，
    才退回 api.github.com；那个接口不登录时每个 IP 每小时只能查 60 次。"""
    data = _manifest() or _from_api()
    tag = data["tag_name"]
    latest, cur = parse(tag), parse(current) or (0, 0, 0)
    if not latest or latest <= cur:
        return None
    version = tag.lstrip("vV")
    info = {"version": version, "notes": data["notes"], "page": data["page"] or RELEASES_URL, "url": None, "sha256": None}
    if mode():
        asset = data["assets"].get(asset_name(version))
        if asset:
            info["url"] = f"https://github.com/{REPO}/releases/download/{tag}/{asset_name(version)}"
            info["sha256"] = asset["sha256"]
    return info


def _manifest():
    """读 update.json；最新 Release 里没有这个文件时返回 None。"""
    try:
        with _get(MANIFEST_URL, "application/json") as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    assets = {}
    for name, a in (data.get("assets") or {}).items():
        sha = str(a.get("sha256") or "").lower()
        assets[name] = {"sha256": sha if len(sha) == 64 else None}
    return {"tag_name": data.get("tag_name") or "", "notes": data.get("body") or "",
            "page": data.get("page") or "", "assets": assets}


def _from_api():
    try:
        with _get(API_URL, "application/vnd.github+json") as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            raise RuntimeError("GitHub 访问次数暂时用完了，请过一小时再检查更新") from e
        raise
    assets = {}
    for a in data.get("assets") or []:
        digest = a.get("digest") or ""
        assets[a.get("name")] = {"sha256": digest[7:].lower() if digest.startswith("sha256:") else None}
    return {"tag_name": data.get("tag_name") or "", "notes": data.get("body") or "",
            "page": data.get("html_url") or "", "assets": assets}


def can_install(info):
    return bool(mode() and info.get("url") and info.get("sha256"))


def can_write_app_dir():
    try:
        probe = APP_DIR / f".update-probe-{os.getpid()}"
        probe.write_text("")
        probe.unlink()
        return True
    except OSError:
        return False


def download(info, progress=None):
    """下载并校验，返回本地文件路径。progress(百分比) 在下载线程里调用。"""
    url = info["url"]
    if not url or not url.startswith(f"https://github.com/{REPO}/releases/download/"):
        raise RuntimeError("下载地址不属于项目仓库")
    if not info.get("sha256"):
        raise RuntimeError("缺少 SHA-256 校验值")
    work = Path(tempfile.gettempdir()) / f"ThaiCaption-update-{info['version']}"
    work.mkdir(parents=True, exist_ok=True)
    target = work / ("update.zip" if mode() == "zip" else "update.exe")
    sha = hashlib.sha256()
    with _get(url) as r, open(target, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while chunk := r.read(1 << 16):
            f.write(chunk)
            sha.update(chunk)
            done += len(chunk)
            if progress and total:
                progress(done * 100 // total)
    if sha.hexdigest() != info["sha256"]:
        target.unlink(missing_ok=True)
        raise RuntimeError("SHA-256 校验失败，下载的文件可能不完整")
    return target


def start_install(path):
    """准备好新文件并启动更新脚本。调用后程序要马上退出。"""
    path = Path(path)
    work = path.parent
    files = work / "files"
    if files.exists():
        import shutil
        shutil.rmtree(files)
    files.mkdir()
    if mode() == "zip":
        with zipfile.ZipFile(path) as z:
            z.extractall(files)
        source = files
        entries = list(files.iterdir())
        if len(entries) == 1 and entries[0].is_dir():   # zip 里只有一个顶层文件夹（泰语字幕翻译\...）
            source = entries[0]
        if not (source / EXE.name).is_file():
            raise RuntimeError(f"安装包里没有 {EXE.name}")
    else:
        source = files
        path.replace(files / EXE.name)   # 单文件版：新 exe 改成当前 exe 的名字，脚本照样覆盖
    script = work / "update.ps1"
    script.write_text(SCRIPT, encoding="utf-8-sig")
    args = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", str(script),
            "-ProcId", str(os.getpid()), "-Source", str(source), "-Target", str(APP_DIR), "-Exe", str(EXE),
            "-Log", str(work / "update.log")]
    # 重新打开的新程序要当成全新启动，不能沿用旧进程的 PyInstaller 环境（单文件版会去找已删除的解压目录）
    env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
    subprocess.Popen(args, cwd=str(work), env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# 等旧进程退出 → 文件夹整个替换、文件逐个覆盖（被占用时重试）→ 重新打开程序。出错写进 update.log，仍然尝试打开程序。
SCRIPT = r"""param([int]$ProcId, [string]$Source, [string]$Target, [string]$Exe, [string]$Log)
$ErrorActionPreference = 'Stop'
function Retry([scriptblock]$Action) {
  for ($i = 0; ; $i++) {
    try { & $Action; return } catch { if ($i -ge 40) { throw }; Start-Sleep -Milliseconds 500 }
  }
}
try {
  try { Wait-Process -Id $ProcId -Timeout 120 } catch { }
  Start-Sleep -Milliseconds 500
  foreach ($item in Get-ChildItem -LiteralPath $Source -Force) {
    $dest = Join-Path $Target $item.Name
    if ($item.PSIsContainer) {
      if (Test-Path -LiteralPath $dest) { Retry { Remove-Item -LiteralPath $dest -Recurse -Force } }
      Retry { Copy-Item -LiteralPath $item.FullName -Destination $dest -Recurse -Force }
    } else {
      Retry { Copy-Item -LiteralPath $item.FullName -Destination $dest -Force }
    }
  }
  'ok ' + (Get-Date) | Out-File -LiteralPath $Log -Encoding utf8
} catch {
  ('failed ' + (Get-Date) + ': ' + $_) | Out-File -LiteralPath $Log -Encoding utf8
}
Start-Process -FilePath $Exe -WorkingDirectory $Target
"""
