"""配置读写：保存在 %APPDATA%\\ThaiCaption\\config.json。"""
import json
import logging
import os
from pathlib import Path

APP_DIR = Path(os.environ.get("THAICAPTION_HOME") or Path(os.environ.get("APPDATA") or Path.home()) / "ThaiCaption")
CONFIG_FILE = APP_DIR / "config.json"
TRANSCRIPT_DIR = APP_DIR / "transcripts"
# 本机部署了模型时，自动读取 llama-swap 的密钥，免得手填
log = logging.getLogger("config")
LOCAL_KEY_FILE = Path(r"E:\llama.cpp\api-key.txt")

DEFAULTS = {
    "api_base": "http://100.95.101.79:8080/v1",
    "api_key": "",
    "model": "hymt2",
    "translate_mode": "local",  # 翻译走哪里：local = 本地 llama-swap；online = 线上 API
    "online_api_base": "https://new.ahei.asia/v1",
    "online_api_key": "",
    "online_model": "gpt-5.6-luna",
    "online_thinking": "off",   # 线上 API 定稿时的思考档位：off = 关闭（最快）；low = 翻得更细但每句多等几秒
    "target_lang": "中文",
    "port": 47321,
    "share_name": "",           # 共享时显示的名称，空则用“电脑名 的字幕”
    "share_port": 47322,        # 局域网观看网页 / 字幕流的端口
    "share_ip": "",             # 二维码里用哪个本机地址（多网卡时）
    "view_code": "",            # 6 位观看码，首次共享时生成，可在设置里重置
    "watch_last": "",           # 上次手动输入的观看地址
    "source": "teams",          # 字幕来源：teams = Teams 字幕（网页版扩展 + 桌面版）；asr = 语音识别（系统声音）
    "asr_model": "whisper-th",  # 语音识别模型（llama-swap 里的名字）
    "asr_language": "th",
    "asr_device": "",           # 录哪个输出设备的声音，空 = 默认扬声器
    "live_preview": True,       # 说话过程中先出临时译文，说完再用定稿覆盖
    "use_context": True,        # 把前两句原文作为上文一起发给模型
    "glossary": "",             # 每行“原文=译文”
    "meeting_topic": "保险行业会议（理赔、查勘、定损、核保、保单、保费、赔款等）",  # 写进通用模型的提示词，空 = 不写
    "theme": 0,
    "debug_log": False,
    "touchpad_speed": 40,       # 触控板滚动速度（%），鼠标滚轮不受影响         # 详细日志：连临时识别 / 临时翻译都记录
    "backdrop": 0,              # 0 云母 / 1 亚克力 / 2 云母 Alt / 3 纯色
    "tint": 55,                 # 亚克力上方遮罩的不透明度（%）
    "overlay_visible": True,
    "overlay_lines": 3,
    "overlay_font": 22,
    "overlay_show_source": True,
    "overlay_opacity": 80,      # 浮窗背景不透明度（%）
    "overlay_blur": False,      # 浮窗毛玻璃背景
    "overlay_geometry": None,   # [x, y, w, h]
    "click_through": False,
}


def load():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    if not cfg["api_key"] and LOCAL_KEY_FILE.exists():
        try:
            cfg["api_key"] = LOCAL_KEY_FILE.read_text(encoding="ascii").strip()
        except OSError:
            pass
    return cfg


def save(cfg):
    """先写临时文件再替换，写到一半断电也不会把配置弄坏。替换失败（有的电脑上会报“句柄无效”）
    就直接写 config.json；都失败只记日志，不抛出，免得退出软件时卡住。"""
    text = json.dumps(cfg, ensure_ascii=False, indent=2)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, CONFIG_FILE)
        return True
    except OSError as e:
        log.warning("保存配置时替换文件失败，改为直接写入：%r", e)
    try:
        CONFIG_FILE.write_text(text, encoding="utf-8")
        tmp.unlink(missing_ok=True)
        return True
    except OSError:
        log.exception("保存配置失败")
        return False


def parse_glossary(text):
    """“原文=译文”每行一条，也接受 Tab 或全角等号分隔；# 开头为注释。"""
    pairs = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for sep in ("=", "＝", "\t"):
            if sep in line:
                src, dst = (s.strip() for s in line.split(sep, 1))
                if src and dst:
                    pairs.append((src, dst))
                break
    return pairs
