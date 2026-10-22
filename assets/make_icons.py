"""从黑底的原图生成各处用的图标：抠掉与边缘连通的黑色背景，裁成正方形。

    python make_icons.py [原图路径]     # 默认 icon_source.png

输出：assets/app.png、assets/app.ico、extension/icon16/48/128.png、app/web/favicon.png
"""
import sys
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def cutout(src):
    im = np.asarray(Image.open(src).convert("RGB")).astype(np.float32)
    mx = im.max(axis=2)
    # 与图片边缘连通的深色区域 = 背景（“文”字的深色笔画在白色气泡里，不与边缘连通，不受影响）
    lab, _ = ndimage.label(mx < 70)
    edge = np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))
    bg = np.isin(lab, edge[edge > 0])
    alpha = np.ones_like(mx)
    alpha[bg] = np.clip((mx[bg] - 12) / 58, 0, 1)
    # 背景边缘的半透明像素原本是“颜色 × 透明度 + 黑色”，还原出原色
    rgb = im.copy()
    semi = bg & (alpha > 0)
    rgb[semi] = np.clip(im[semi] / alpha[semi][:, None], 0, 255)
    out = Image.fromarray(np.dstack([rgb, alpha * 255]).astype(np.uint8))
    ys, xs = np.where(alpha > 0.05)
    crop = out.crop((xs.min(), ys.min(), xs.max() + 1, ys.max() + 1))
    side = int(max(crop.size) * 1.08)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(crop, ((side - crop.width) // 2, (side - crop.height) // 2))
    return canvas.resize((512, 512), Image.LANCZOS)


def main():
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "icon_source.png"
    icon = cutout(src)
    icon.save(HERE / "app.png")
    icon.save(HERE / "app.ico", sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    for s in (16, 48, 128):
        icon.resize((s, s), Image.LANCZOS).save(ROOT / "extension" / f"icon{s}.png")
    icon.resize((192, 192), Image.LANCZOS).save(ROOT / "app" / "web" / "favicon.png")
    print("图标已生成")


if __name__ == "__main__":
    main()
