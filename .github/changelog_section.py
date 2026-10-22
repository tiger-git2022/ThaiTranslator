"""输出 CHANGELOG.md 里某个版本的说明，再附上下载说明，作为 GitHub Release 的正文。
用法：python .github/changelog_section.py 1.5.7 [输出文件]，不给输出文件就打印出来。"""
import re
import sys
from pathlib import Path

version = sys.argv[1]
text = Path(__file__).resolve().parent.parent.joinpath("CHANGELOG.md").read_text(encoding="utf-8")
m = re.search(rf"^## v{re.escape(version)}\b[^\n]*\n(.*?)(?=^## |\Z)", text, re.S | re.M)
body = m.group(1).strip() if m else "（CHANGELOG.md 里还没有这个版本的说明）"

notes = (f"""{body}

### 下载哪个

| 文件 | 说明 |
| --- | --- |
| `ThaiCaption-{version}-windows-x64.zip` | **推荐**。解压后打开 `泰语字幕翻译` 文件夹，双击 `泰语字幕翻译.exe`。启动快，浏览器扩展也在里面。 |
| `ThaiCaption-{version}-windows-x64-onefile.exe` | 单文件版，下载即用。每次启动要先解压到临时目录，会慢几秒。 |

只支持 Windows 10 / 11（64 位）。首次运行如果 SmartScreen 拦截，点“更多信息 → 仍要运行”。""")
if len(sys.argv) > 2:
    Path(sys.argv[2]).write_text(notes + "\n", encoding="utf-8")
else:
    sys.stdout.reconfigure(encoding="utf-8")
    print(notes)
