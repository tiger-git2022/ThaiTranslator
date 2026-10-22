"""输出 CHANGELOG.md 里某个版本的说明，再附上下载说明，作为 GitHub Release 的正文。
用法：python .github/changelog_section.py 1.5.8 [输出文件]，不给输出文件就打印出来。"""
import re
import sys
from pathlib import Path

version = sys.argv[1]
text = Path(__file__).resolve().parent.parent.joinpath("CHANGELOG.md").read_text(encoding="utf-8")
m = re.search(rf"^## v{re.escape(version)}\b[^\n]*\n(.*?)(?=^## |\Z)", text, re.S | re.M)
body = m.group(1).strip() if m else "（CHANGELOG.md 里还没有这个版本的说明）"

notes = f"""{body}

### 下载哪个

| 文件 | 说明 |
| --- | --- |
| `ThaiCaption-{version}-windows-x64.zip` | **Windows 推荐**。解压后打开 `泰语字幕翻译` 文件夹，双击 `泰语字幕翻译.exe`。启动快，浏览器扩展也在里面。 |
| `ThaiCaption-{version}-windows-x64-onefile.exe` | Windows 单文件版，下载即用。每次启动要先解压到临时目录，会慢几秒。 |
| `ThaiCaption-{version}-macos-arm64.zip` | Mac（Apple 芯片：M1、M2、M3、M4…）。 |
| `ThaiCaption-{version}-macos-x64.zip` | Mac（Intel 芯片）。不确定是哪种：点屏幕左上角苹果菜单 →“关于本机”，看“芯片”或“处理器”。 |

**Windows**：支持 Windows 10 / 11（64 位）。首次运行如果 SmartScreen 拦截，点“更多信息 → 仍要运行”。

**Mac**：支持 macOS 12 及以上。Mac 版目前只能读取 **Teams 网页版**（Chrome / Edge）的字幕，翻译、浮窗、局域网共享都能用；Teams 桌面版字幕和语音识别只有 Windows 版有。
软件没有苹果开发者签名，第一次打开要多点一步：
1. 解压后把 `泰语字幕翻译.app` 拖进“应用程序”文件夹，双击打开，看到“无法验证开发者”的提示时点“完成”。
2. 打开“系统设置 → 隐私与安全性”，拉到最下面，点“仍要打开”，再输一次开机密码。之后就能正常双击打开了。
3. 如果提示“已损坏，无法打开”，打开“终端”，粘贴下面这行并回车，再双击打开：
   `xattr -dr com.apple.quarantine /Applications/泰语字幕翻译.app`"""

if len(sys.argv) > 2:
    Path(sys.argv[2]).write_text(notes + "\n", encoding="utf-8")
else:
    sys.stdout.reconfigure(encoding="utf-8")
    print(notes)
