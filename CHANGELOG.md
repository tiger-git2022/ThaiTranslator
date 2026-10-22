# 更新日志

每个版本一节，标题写成 `## v版本号`。打标签发布时，GitHub Actions 会把对应的一节作为 Release 说明。

## v1.5.8

- 新增 **Mac 版**（Apple 芯片和 Intel 芯片各一个）：用 Chrome / Edge 打开 Teams 网页版开会，通过浏览器扩展读取字幕，翻译、置顶浮窗、局域网共享和观看共享都能用。Teams 桌面版字幕和语音识别暂时只有 Windows 版支持。
- 代码里不再内置 tiger-pc 的地址，新电脑第一次使用时在“设置 → 翻译服务”里填写接口地址。已经用过的电脑不受影响。
- 每次发布前在 Windows 和 Mac 上自动做启动自检。

## v1.5.7

- 首次在 GitHub 发布，由 GitHub Actions 自动打包文件夹版（zip）和单文件版（exe）。
