"""生成 update.json，跟安装包一起上传到 Release。

程序检查更新时读 https://github.com/<仓库>/releases/latest/download/update.json。
这个地址和下载安装包一样，没有次数限制；api.github.com 不登录时每个 IP 每小时只能查 60 次，
用完就返回 403 rate limit exceeded。

用法：python update_manifest.py <版本号> <Release 说明文件> <安装包所在文件夹>
结果写到 <安装包所在文件夹>/update.json，内容：
{"tag_name", "version", "body", "page", "assets": {文件名: {"size", "sha256"}}}
"""
import hashlib
import json
import os
import sys
from pathlib import Path

version, notes_path, out_dir = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
repo = os.environ.get("GITHUB_REPOSITORY", "tiger-git2022/ThaiTranslator")
tag = f"v{version}"

assets = {}
for f in sorted(out_dir.iterdir()):
    if f.suffix.lower() not in (".zip", ".exe"):
        continue
    sha = hashlib.sha256()
    with open(f, "rb") as fh:
        while chunk := fh.read(1 << 20):
            sha.update(chunk)
    assets[f.name] = {"size": f.stat().st_size, "sha256": sha.hexdigest()}
if not assets:
    sys.exit(f"{out_dir} 里没有安装包")

manifest = {
    "tag_name": tag,
    "version": version,
    "body": notes_path.read_text(encoding="utf-8"),
    "page": f"https://github.com/{repo}/releases/tag/{tag}",
    "assets": assets,
}
target = out_dir / "update.json"
target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
print(target.read_text(encoding="utf-8"))
