# -*- coding: utf-8 -*-
"""
YouTube 字幕助手  v1.0
======================
给一个 YouTube 链接，最终一定能拿到字幕（SRT / VTT / 纯文本），并能下载保存。
视频本身可下可不下，默认不下，只下音频做识别，省时间省空间。

参考了开源项目 VideoCaptioner (WEIFENG2333/VideoCaptioner, GPL-3.0) 的思路，
但补了它没有的一步：它只有 `download 视频` + `transcribe 本地文件` 两段，
没有“先拿 YouTube 现成字幕，没有再识别”的一键流程。本程序把三段接起来：

  第 1 步（快，几秒）：先拿 YouTube 官方字幕 / 自动生成字幕（yt-dlp）
  第 2 步（慢，几分钟）：没有官方字幕，才只下载音频，用 faster-whisper 本地识别
  第 3 步（可选）：勾选后才下载完整视频文件

全程不需要 API Key，不需要把视频发给任何在线服务。
识别在你自己电脑上跑，模型第一次用时自动下载一次（tiny 约 75MB / base 约 145MB）。

用法（图形界面，双击即可）：
    python yt_subtitle.py
用法（命令行）：
    python yt_subtitle.py "https://www.youtube.com/watch?v=xxxx"
    python yt_subtitle.py "https://youtu.be/xxxx" --lang zh --model base --download-video
    python yt_subtitle.py --file 本地视频.mp4 --lang zh
    python yt_subtitle.py --self-test        # 不联网，自测 VTT 转 SRT

依赖（见 requirements.txt）：
    yt-dlp, faster-whisper, av==12.3.0（av 新版会和 faster-whisper 冲突，必须钉住）
    系统里要有 ffmpeg（yt-dlp 转音频、合并视频要用）
    Windows 上没有 ffmpeg 时，安装脚本会用 imageio-ffmpeg 自动带一个。
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

VERSION = "1.0.4"

# ---------------------------------------------------------------- 工具函数

def log_print(msg, log_fn=None):
    """统一日志：有界面时写界面，没有就打印。"""
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    if log_fn:
        log_fn(line)
    else:
        print(line, flush=True)


def format_srt_time(seconds):
    """秒 -> SRT 时间戳 00:01:23,456"""
    seconds = max(0.0, float(seconds))
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def format_vtt_time(seconds):
    return format_srt_time(seconds).replace(",", ".")


def parse_vtt_time(text):
    """VTT/SRT 时间戳 -> 秒，接受 00:01:23.456 / 01:23.456 / 01:23,456"""
    text = text.strip().replace(",", ".")
    parts = text.split(":")
    try:
        if len(parts) == 3:
            h, m, s = parts
            return int(h) * 3600 + int(m) * 60 + float(s)
        if len(parts) == 2:
            m, s = parts
            return int(m) * 60 + float(s)
        return float(parts[0])
    except ValueError:
        return None


def clean_cue_text(text):
    """去掉 VTT 里 <c.color>、<b>、时间戳标签 <00:00:01.000> 这类标记，只留文字。"""
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&nbsp;", " ")
    return text.strip()


def vtt_to_segments(vtt_text):
    """把 VTT 文本解析成 [{'start': 秒, 'end': 秒, 'text': 文字}]。"""
    segments = []
    # 去掉 WEBVTT 头、NOTE / STYLE / REGION 块
    lines = vtt_text.splitlines()
    buf = []
    skip_block = False
    for line in lines:
        s = line.strip()
        if s.startswith("WEBVTT") or s.startswith("Kind:") or s.startswith("Language:"):
            continue
        if s in ("NOTE", "STYLE", "REGION"):
            skip_block = True
            continue
        if skip_block:
            if s == "":
                skip_block = False
            continue
        buf.append(line)
    text = "\n".join(buf)
    # 按空行分块
    blocks = re.split(r"\n\s*\n", text.strip())
    ts_re = re.compile(
        r"(\d{1,2}:\d{2}:\d{2}[.,]\d{3}|\d{1,2}:\d{2}[.,]\d{3})\s*-->\s*"
        r"(\d{1,2}:\d{2}:\d{2}[.,]\d{3}|\d{1,2}:\d{2}[.,]\d{3})"
    )
    for block in blocks:
        block_lines = [ln for ln in block.splitlines() if ln.strip()]
        if not block_lines:
            continue
        ts_idx, start, end = None, None, None
        for i, ln in enumerate(block_lines):
            m = ts_re.search(ln)
            if m:
                start = parse_vtt_time(m.group(1))
                end = parse_vtt_time(m.group(2))
                ts_idx = i
                break
        if ts_idx is None or start is None or end is None:
            continue
        cue_lines = [clean_cue_text(ln) for ln in block_lines[ts_idx + 1:]]
        cue_lines = [c for c in cue_lines if c]
        if not cue_lines:
            continue
        segments.append({"start": start, "end": end, "text": "\n".join(cue_lines)})
    # 按开始时间排序，去掉完全重复的相邻行（自动字幕常有重复）
    segments.sort(key=lambda s: s["start"])
    deduped = []
    for seg in segments:
        if deduped and deduped[-1]["text"] == seg["text"] and seg["start"] < deduped[-1]["end"] + 0.01:
            deduped[-1]["end"] = max(deduped[-1]["end"], seg["end"])
            continue
        deduped.append(seg)
    return deduped


def segments_to_srt(segments):
    out = []
    for i, seg in enumerate(segments, 1):
        out.append(str(i))
        out.append(f"{format_srt_time(seg['start'])} --> {format_srt_time(seg['end'])}")
        out.append(seg["text"])
        out.append("")
    return "\n".join(out)


def segments_to_vtt(segments):
    out = ["WEBVTT", ""]
    for seg in segments:
        out.append(f"{format_vtt_time(seg['start'])} --> {format_vtt_time(seg['end'])}")
        out.append(seg["text"])
        out.append("")
    return "\n".join(out)


def segments_to_txt(segments):
    return "\n".join(seg["text"] for seg in segments)


def write_subtitle_files(segments, out_base: Path):
    """写出 .srt / .vtt / .txt 三份，返回写出的文件列表。"""
    out_base.parent.mkdir(parents=True, exist_ok=True)
    srt_path = out_base.with_suffix(".srt")
    vtt_path = out_base.with_suffix(".vtt")
    txt_path = out_base.with_suffix(".txt")
    srt_path.write_text(segments_to_srt(segments), encoding="utf-8")
    vtt_path.write_text(segments_to_vtt(segments), encoding="utf-8")
    txt_path.write_text(segments_to_txt(segments), encoding="utf-8")
    return [srt_path, vtt_path, txt_path]


def extract_video_id(url):
    """从各种 YouTube 链接里取视频 ID，取不到返回 None。"""
    if not url:
        return None
    url = url.strip()
    patterns = [
        r"[?&]v=([A-Za-z0-9_-]{11})",
        r"youtu\.be/([A-Za-z0-9_-]{11})",
        r"/shorts/([A-Za-z0-9_-]{11})",
        r"/embed/([A-Za-z0-9_-]{11})",
        r"/live/([A-Za-z0-9_-]{11})",
        r"/v/([A-Za-z0-9_-]{11})",
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url):
        return url
    return None


def safe_filename(name, default="youtube_video"):
    name = re.sub(r'[\\/:*?"<>|]', "_", (name or "").strip())
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:120] or default


def check_ffmpeg():
    return find_ffmpeg() is not None


def find_ffmpeg():
    """找 ffmpeg：先找系统 PATH 里的，再找 imageio-ffmpeg 带的那个。
    Windows 一键安装脚本没装系统 ffmpeg 时就靠后者。"""
    path = shutil.which("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).exists():
            return exe
    except Exception:
        pass
    return None


class _QuietYDLLogger:
    """接管 yt-dlp 的日志，不让它往控制台刷红字 ERROR。
    真正的失败由调用方的异常处理用人话报出来（弹窗里那句），
    这里不需要再重复刷一遍原始报错。"""

    def debug(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        pass


def get_ydl_opts_base(out_dir, cookies_file=None, cookies_from_browser=None,
                      player_clients=None):
    opts = {"noplaylist": True, "quiet": True, "no_warnings": True,
            "logger": _QuietYDLLogger()}
    # 有 cookies.txt 文件就只用文件，不再碰浏览器：
    # 浏览器还开着时 Cookie 数据库是锁住的，yt-dlp 拷不出来会直接报错，
    # 而文件方式不需要关浏览器。两样都填时文件优先。
    if cookies_file:
        opts["cookiefile"] = str(cookies_file)
    elif cookies_from_browser:
        opts["cookiesfrombrowser"] = (cookies_from_browser,)
    if player_clients:
        # 字幕和音频用安卓端去要：网页端有时拿不到这个视频的格式列表
        # （报 Requested format is not available），安卓端通常还能拿到。
        # 完整视频下载不用这个，免得限制画质。
        opts["extractor_args"] = {"youtube": {"player_client": list(player_clients)}}
    return opts


def is_bot_check_error(msg):
    msg = (msg or "").lower()
    return ("sign in to confirm" in msg or "not a bot" in msg
            or "confirm you’re not a bot" in msg or "confirm you're not a bot" in msg)


def friendly_ydl_error(msg):
    """把 yt-dlp 又长又 technical 的报错，缩成一句人话。返回 (是否是机器人验证, 人话)。"""
    raw = (msg or "").strip()
    # yt-dlp 报错里常带 ANSI 颜色码（[0;31m 这种），先去掉，不然弹窗里全是乱码
    raw = re.sub(r"\x1b\[[0-9;]*m", "", raw)
    raw = re.sub(r"ERROR:\s*", "", raw)
    # yt-dlp 报错常带一长串说明，只留第一段
    first = raw.split("\n")[0].strip()
    if len(first) > 300:
        first = first[:300] + "…"
    if is_bot_check_error(raw):
        return True, (
            "BOT_CHECK: YouTube 要求登录验证（Sign in to confirm you're not a bot）。"
            "这不是程序坏了，是 YouTube 在拦这台电脑的 IP。"
            "办法：在界面上选“用浏览器 Cookie”（Chrome/Edge，须先在浏览器里登录 YouTube），"
            "或导出 cookies.txt 再试；换家庭宽带也常能直接过。")
    low = raw.lower()
    if "could not copy" in low and "cookie" in low:
        return False, (
            "读不到 Chrome 的 Cookie：Chrome 还开着，Cookie 数据库被锁住了，拷不出来。"
            "办法二选一：① 把 Chrome 完全关掉再点开始；"
            "② 用 cookies.txt 文件（浏览器 Cookie 改回“不使用”，只留 cookies.txt 那栏），"
            "文件方式不用关浏览器。")
    if "requested format is not available" in low:
        return False, (
            "YouTube 没给到可下载的格式（网页端拿不到这个视频的格式列表）。"
            "这不是 Cookie 的问题，Cookie 已经生效了。程序会改用安卓端再要一次，"
            "还不行就自动改走第 2 步：只下音频做识别。原始报错：" + first)
    if "does not look like a netscape format cookies file" in low:
        return False, (
            "你选的 cookies.txt 文件格式不对（不是 Netscape 格式）。"
            "请确认选的是浏览器扩展导出的 cookies.txt，不要选 requirements.txt 之类的其他文件。"
            "原始报错：" + first)
    if "certificate_verify_failed" in low or "self-signed certificate" in low:
        return False, (
            "网络证书验证失败：这台电脑的网络（公司网络 / 代理 / 安全软件）在拦截 YouTube 的证书，"
            "yt-dlp 连不上。办法：换家庭宽带或手机热点再试，或关掉代理/VPN；"
            "家里电脑正常上网一般不会遇到这个。原始报错：" + first)
    if "429" in raw or "too many requests" in low:
        return False, (
            "YouTube 限流了（429 Too Many Requests）：这台 IP 短时间内请求太多。"
            "办法：等一会再试，或用浏览器 Cookie 再试。原始报错：" + first)
    return False, first


# ---------------------------------------------------------------- 第 1 步：官方字幕

LANG_SUBS = {
    "zh": ["zh-Hans", "zh-Hant", "zh-CN", "zh-TW", "zh"],
    "en": ["en"],
    "auto": ["zh-Hans", "zh-Hant", "zh-CN", "zh", "en"],
}


def pick_subtitle_file(files, lang):
    """在下载到的字幕文件里按优先级挑一个：手动中文 > 自动中文 > 手动英文 > 自动英文。"""
    if not files:
        return None
    def score(p: Path):
        name = p.name.lower()
        is_auto = ".auto." in name or "auto" in p.suffixes.__str__()
        # yt-dlp 的自动字幕文件名里带 .auto.，手动的不带
        auto = 1 if ".auto." in name else 0
        if ".zh-hans." in name or ".zh-cn." in name or ".zh." in name:
            lang_score = 0
        elif ".zh-hant." in name or ".zh-tw." in name:
            lang_score = 1
        elif ".en." in name:
            lang_score = 2
        else:
            lang_score = 3
        if lang == "en":
            lang_score = 0 if ".en." in name else 3
        return (lang_score, auto)
    return sorted(files, key=score)[0]


def fetch_official_subs(url, out_dir: Path, lang="auto", cookies_file=None,
                        cookies_from_browser=None, log_fn=None):
    """返回 (segments, 标题, 字幕文件)；没有官方字幕返回 (None, 标题, None)。
    遇到机器人验证会抛 RuntimeError('BOT_CHECK: ...')，让上层给用户明确提示。"""
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("缺少 yt-dlp，请先运行一键安装脚本（见使用说明）。")
    video_id = extract_video_id(url) or "video"
    out_dir.mkdir(parents=True, exist_ok=True)
    sub_langs = LANG_SUBS.get(lang, LANG_SUBS["auto"])
    ydl_opts = get_ydl_opts_base(out_dir, cookies_file, cookies_from_browser,
                                player_clients=["android", "web"])
    ydl_opts.update({
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsubtitles": True,
        "subtitleslangs": sub_langs,
        "subtitlesformat": "vtt/best",
        "outtmpl": str(out_dir / "%(title)s.%(id)s.%(ext)s"),
    })
    log_print(f"第 1 步：正在向 YouTube 要官方字幕（优先 {','.join(sub_langs)}）…", log_fn)
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as e:
        _, friendly = friendly_ydl_error(str(e))
        raise RuntimeError(f"取官方字幕失败：{friendly}")
    title = safe_filename((info or {}).get("title") or video_id)
    # yt-dlp 下载的字幕文件：标题.视频ID.语言.vtt（自动字幕带 .auto.）
    candidates = sorted(out_dir.glob(f"*{video_id}*.vtt"))
    if not candidates:
        # 有的版本 outtmpl 不带 id，再按标题兜底找
        candidates = sorted(out_dir.glob("*.vtt"))
    chosen = pick_subtitle_file(candidates, lang)
    if not chosen:
        log_print("YouTube 上没有这个视频的官方字幕 / 自动字幕，改走第 2 步识别。", log_fn)
        return None, title, None
    segments = vtt_to_segments(chosen.read_text(encoding="utf-8", errors="ignore"))
    if not segments:
        log_print(f"官方字幕文件 {chosen.name} 解析出来是空的，改走第 2 步识别。", log_fn)
        return None, title, None
    kind = "自动生成字幕" if ".auto." in chosen.name.lower() else "官方人工字幕"
    log_print(f"拿到 {kind}：{chosen.name}，共 {len(segments)} 条。", log_fn)
    return segments, title, chosen


# ---------------------------------------------------------------- 第 2 步：下载音频 + 本地识别

def download_audio(url, out_dir: Path, cookies_file=None, cookies_from_browser=None,
                   log_fn=None):
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("缺少 yt-dlp，请先运行一键安装脚本。")
    if not check_ffmpeg():
        raise RuntimeError("找不到 ffmpeg：转音频需要它。Windows 请运行一键安装脚本，它会自动装；"
                           "或手动装好 ffmpeg 并加到 PATH。")
    ffmpeg_path = find_ffmpeg()
    out_dir.mkdir(parents=True, exist_ok=True)
    ydl_opts = get_ydl_opts_base(out_dir, cookies_file, cookies_from_browser,
                                player_clients=["android", "web"])
    if ffmpeg_path:
        ydl_opts["ffmpeg_location"] = str(Path(ffmpeg_path).parent)
    ydl_opts.update({
        "format": "bestaudio/best",
        "outtmpl": str(out_dir / "%(title)s.%(id)s.%(ext)s"),
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    })
    log_print("第 2 步：只下载音频（不下完整视频，省时间省空间）…", log_fn)
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as e:
        _, friendly = friendly_ydl_error(str(e))
        raise RuntimeError(
            f"下载音频失败：{friendly}\n"
            "另一条路：先用浏览器把这个视频下成 mp4（或 mp3），"
            "再点【转写本地音视频文件…】选它转写——本地识别那步你已经跑通过了。")
    title = safe_filename((info or {}).get("title") or extract_video_id(url) or "video")
    video_id = extract_video_id(url) or ""
    audios = sorted(out_dir.glob(f"*{video_id}*.mp3")) if video_id else []
    if not audios:
        audios = sorted(out_dir.glob("*.mp3"))
    if not audios:
        raise RuntimeError("音频下载完了但没找到 mp3 文件，请看日志里的 yt-dlp 报错。")
    # 取最新改动的那个（避免旧文件干扰）
    audio = max(audios, key=lambda p: p.stat().st_mtime)
    log_print(f"音频已下载：{audio.name}", log_fn)
    return audio, title


def transcribe_audio(audio_path: Path, model="base", lang="auto", log_fn=None):
    """用 faster-whisper 本地识别，返回 segments 列表。"""
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise RuntimeError(f"缺少 faster-whisper（{e}）。请先运行一键安装脚本。")
    whisper_lang = None if lang in (None, "auto", "") else lang
    log_print(f"正在本地识别：模型 {model}，语言 {'自动检测' if not whisper_lang else whisper_lang}…"
              "第一次用要下载模型，耐心等一会，之后就不用再下了。", log_fn)
    try:
        model_obj = WhisperModel(model, device="cpu", compute_type="int8")
    except Exception as e:
        raise RuntimeError(f"加载识别模型 {model} 失败：{e}。可试试更小的 tiny 模型。")
    segments, info = model_obj.transcribe(
        str(audio_path), language=whisper_lang, vad_filter=True, beam_size=5)
    out = []
    for i, seg in enumerate(segments):
        out.append({"start": float(seg.start), "end": float(seg.end),
                    "text": (seg.text or "").strip()})
        if (i + 1) % 50 == 0:
            log_print(f"已识别 {i + 1} 条…", log_fn)
    out = [s for s in out if s["text"]]
    detected = getattr(info, "language", "?")
    prob = getattr(info, "language_probability", 0)
    log_print(f"识别完成：检测到语言 {detected}（可信度 {prob:.2f}），共 {len(out)} 条。", log_fn)
    if not out:
        raise RuntimeError("识别结果是空的：音频里可能没有人声，或音频文件损坏。")
    return out


# ---------------------------------------------------------------- 第 3 步（可选）：下载完整视频

def download_video(url, out_dir: Path, cookies_file=None, cookies_from_browser=None,
                   log_fn=None):
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("缺少 yt-dlp，请先运行一键安装脚本。")
    out_dir.mkdir(parents=True, exist_ok=True)
    ydl_opts = get_ydl_opts_base(out_dir, cookies_file, cookies_from_browser)
    ffmpeg_path = find_ffmpeg()
    if ffmpeg_path:
        ydl_opts["ffmpeg_location"] = str(Path(ffmpeg_path).parent)
    ydl_opts.update({
        "format": "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "outtmpl": str(out_dir / "%(title)s.%(id)s.%(ext)s"),
    })
    log_print("正在下载完整视频（bestvideo+bestaudio，合并为 mp4）…", log_fn)
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        _, friendly = friendly_ydl_error(str(e))
        raise RuntimeError(f"下载视频失败：{friendly}")
    log_print("完整视频已下载。", log_fn)


def _looks_like_netscape_cookies(path: Path):
    """粗判一个文件是不是 Netscape 格式的 cookies.txt。
    Netscape 格式：注释行以 # 开头，数据行是 Tab 分隔、至少 7 列，首列是域名。
    requirements.txt 这类文件没有这种数据行，一眼就能识破，
    免得用户选错文件后只看到 yt-dlp 的英文报错。"""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7 and "." in parts[0]:
            return True
    return False


# ---------------------------------------------------------------- 主流程

def run_youtube(url, out_dir: Path, lang="auto", model="base", mode="auto",
                download_video_flag=False, keep_audio=False,
                cookies_file=None, cookies_from_browser=None, log_fn=None):
    """mode: auto=优先官方字幕，没有再识别；official=只要官方字幕，没有就报错；
    whisper=跳过官方字幕，直接识别。返回结果字典。"""
    url = (url or "").strip()
    if not url or not extract_video_id(url):
        raise ValueError("链接不对：请粘贴完整的 YouTube 链接，如 https://www.youtube.com/watch?v=xxxx 或 https://youtu.be/xxxx")
    if cookies_file and not Path(cookies_file).is_file():
        raise ValueError(f"cookies.txt 找不到：{cookies_file}。请确认选的是导出的 cookies.txt 文件（不是文件夹），"
                         "一般在下载文件夹里，名字是 cookies.txt。")
    if cookies_file and not _looks_like_netscape_cookies(Path(cookies_file)):
        raise ValueError(
            f"你选的这个文件不是 cookies.txt：{cookies_file}。"
            "请选浏览器扩展导出的那个文件：点“选择…”后，左边点“下载”，不要进任何文件夹，"
            "直接选名字就是 cookies.txt 的那个；不要选 requirements.txt 或其他文件。")
    out_dir = Path(out_dir)
    tmp_dir = out_dir / "_temp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    title = extract_video_id(url)
    segments = None
    source = None
    if mode in ("auto", "official"):
        try:
            segments, title, _sub_file = fetch_official_subs(
                url, tmp_dir, lang=lang, cookies_file=cookies_file,
                cookies_from_browser=cookies_from_browser, log_fn=log_fn)
        except Exception as e:
            if mode == "official":
                raise
            # 第 1 步拿不到不直接报错：改走第 2 步只下音频识别，
            # 只有两步都失败才算真的失败。
            log_print(f"第 1 步没拿到官方字幕：{e} 改走第 2 步识别。", log_fn)
            segments = None
        if segments:
            source = "youtube_official"
    if segments is None:
        if mode == "official":
            raise RuntimeError("这个视频在 YouTube 上没有官方字幕 / 自动字幕。"
                               "把模式改成“自动（没有就识别）”或“直接识别”，就能用语音识别生成。")
        audio_path, title = download_audio(
            url, tmp_dir, cookies_file=cookies_file,
            cookies_from_browser=cookies_from_browser, log_fn=log_fn)
        segments = transcribe_audio(audio_path, model=model, lang=lang, log_fn=log_fn)
        source = "whisper_local"
        if not keep_audio:
            try:
                audio_path.unlink()
            except OSError:
                pass
    out_base = out_dir / f"{title}"
    files = write_subtitle_files(segments, out_base)
    # 清掉临时的字幕下载件，只留最终三份
    for p in tmp_dir.glob("*.vtt"):
        try:
            p.unlink()
        except OSError:
            pass
    video_path = None
    if download_video_flag:
        download_video(url, out_dir, cookies_file=cookies_file,
                       cookies_from_browser=cookies_from_browser, log_fn=log_fn)
        video_path = out_dir
    return {"title": title, "source": source, "segments": len(segments),
            "files": [str(p) for p in files], "out_dir": str(out_dir),
            "video_downloaded": bool(download_video_flag)}


def run_local_file(file_path, out_dir: Path, lang="auto", model="base", log_fn=None):
    """转写本地音视频文件（mp4 / mp3 / wav 等），不经过 YouTube。"""
    file_path = Path(file_path)
    if not file_path.exists():
        raise ValueError(f"找不到本地文件：{file_path}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_print(f"转写本地文件：{file_path.name}", log_fn)
    segments = transcribe_audio(file_path, model=model, lang=lang, log_fn=log_fn)
    out_base = out_dir / file_path.stem
    files = write_subtitle_files(segments, out_base)
    return {"title": file_path.stem, "source": "whisper_local",
            "segments": len(segments), "files": [str(p) for p in files],
            "out_dir": str(out_dir), "video_downloaded": False}


# ---------------------------------------------------------------- 自测（不联网）

SAMPLE_VTT = """WEBVTT
Kind: captions
Language: zh-Hans

00:00:01.000 --> 00:00:04.000
你好，这是字幕测试。

00:00:04.500 --> 00:00:08.000
<c.colorCCCCCC>今天天气不错</c>，我们去看看新车。
"""

def self_test(log_fn=None):
    segs = vtt_to_segments(SAMPLE_VTT)
    assert len(segs) == 2, f"VTT 解析条数不对：{len(segs)}"
    assert segs[0]["text"] == "你好，这是字幕测试。", segs[0]
    assert segs[1]["text"] == "今天天气不错，我们去看看新车。", segs[1]
    assert abs(segs[0]["start"] - 1.0) < 0.01 and abs(segs[0]["end"] - 4.0) < 0.01
    srt = segments_to_srt(segs)
    assert "00:00:01,000 --> 00:00:04,000" in srt
    assert segments_to_txt(segs).count("\n") == 1
    assert extract_video_id("https://www.youtube.com/watch?v=4hi93cDfVls") == "4hi93cDfVls"
    assert extract_video_id("https://youtu.be/4hi93cDfVls") == "4hi93cDfVls"
    assert extract_video_id("https://www.youtube.com/shorts/4hi93cDfVls") == "4hi93cDfVls"
    log_print("自测通过：VTT 解析、SRT/VTT/TXT 生成、链接解析都正常。", log_fn)
    return True


# ---------------------------------------------------------------- 命令行

def build_parser():
    p = argparse.ArgumentParser(
        description="YouTube 字幕助手：给链接就出字幕（SRT/VTT/TXT），视频可下可不下。")
    p.add_argument("url", nargs="?", help="YouTube 视频链接")
    p.add_argument("--file", dest="file", default=None, help="转写本地音视频文件，不经过 YouTube")
    p.add_argument("--out", default="output", help="输出文件夹（默认 ./output）")
    p.add_argument("--lang", default="auto", choices=["auto", "zh", "en"],
                   help="字幕语言：auto 自动，zh 中文，en 英文（默认 auto）")
    p.add_argument("--model", default="base", choices=["tiny", "base", "small", "medium"],
                   help="识别模型：tiny 最快最不准，base 均衡（默认），small 更准更慢")
    p.add_argument("--mode", default="auto", choices=["auto", "official", "whisper"],
                   help="auto=优先官方字幕，没有再识别（默认）；official=只要官方字幕；whisper=直接识别")
    p.add_argument("--download-video", action="store_true", help="同时下载完整视频（默认不下）")
    p.add_argument("--keep-audio", action="store_true", help="保留下载的音频 mp3（默认删掉）")
    p.add_argument("--cookies", default=None, help="cookies.txt 文件路径（过机器人验证用）")
    p.add_argument("--cookies-from-browser", default=None,
                   choices=["chrome", "edge", "firefox", "brave"],
                   help="直接用浏览器的登录 Cookie（须先在该浏览器里登录 YouTube）")
    p.add_argument("--self-test", action="store_true", help="不联网自测，通过就退出")
    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return p


def main_cli(argv=None):
    args = build_parser().parse_args(argv)
    if args.self_test:
        self_test()
        return 0
    try:
        if args.file:
            result = run_local_file(args.file, Path(args.out), lang=args.lang,
                                    model=args.model)
        elif args.url:
            result = run_youtube(
                args.url, Path(args.out), lang=args.lang, model=args.model,
                mode=args.mode, download_video_flag=args.download_video,
                keep_audio=args.keep_audio,
                cookies_file=args.cookies,
                cookies_from_browser=args.cookies_from_browser)
        else:
            build_parser().print_help()
            print("\n没给链接也没给文件：直接运行 python yt_subtitle.py 会打开图形界面。")
            return 2
    except RuntimeError as e:
        print(f"\n❌ {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"\n❌ {e}", file=sys.stderr)
        return 2
    source_name = {"youtube_official": "YouTube 官方字幕",
                   "whisper_local": "本地语音识别"}.get(result["source"], result["source"])
    print(f"\n✅ 完成：{result['title']}")
    print(f"   来源：{source_name}，共 {result['segments']} 条")
    for f in result["files"]:
        print(f"   {f}")
    return 0


# ---------------------------------------------------------------- 图形界面（Tkinter，Python 自带，不用另装）

def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk

    root = tk.Tk()
    root.title(f"YouTube 字幕助手 v{VERSION}")
    root.geometry("720x620")

    url_var = tk.StringVar()
    out_var = tk.StringVar(value=str(Path.cwd() / "output"))
    lang_var = tk.StringVar(value="auto")
    model_var = tk.StringVar(value="base")
    mode_var = tk.StringVar(value="auto")
    cookies_var = tk.StringVar()
    browser_var = tk.StringVar(value="不使用")
    download_video_var = tk.BooleanVar(value=False)
    keep_audio_var = tk.BooleanVar(value=False)
    status_var = tk.StringVar(value="就绪：粘贴链接，点“开始生成”。")

    def log_fn(line):
        def _append():
            log_box.configure(state="normal")
            log_box.insert("end", line + "\n")
            log_box.see("end")
            log_box.configure(state="disabled")
            status_var.set(line)
        root.after(0, _append)

    def browse_out():
        d = filedialog.askdirectory(initialdir=out_var.get() or str(Path.cwd()))
        if d:
            out_var.set(d)

    def browse_cookies():
        f = filedialog.askopenfilename(
            title="选择 cookies.txt",
            filetypes=[("Cookie 文件", "*.txt"), ("所有文件", "*.*")])
        if f:
            cookies_var.set(f)

    def browse_local_file():
        f = filedialog.askopenfilename(
            title="选择本地音视频文件",
            filetypes=[("音视频", "*.mp4 *.mkv *.mov *.mp3 *.wav *.m4a *.flac"),
                       ("所有文件", "*.*")])
        if f:
            run_in_thread(lambda: do_local(f))

    def do_local(file_path):
        try:
            result = run_local_file(file_path, Path(out_var.get()), lang=lang_var.get(),
                                    model=model_var.get(), log_fn=log_fn)
            finish_ok(result)
        except Exception as e:
            finish_fail(e)

    def finish_ok(result):
        def _done():
            start_btn.configure(state="normal")
            source_name = {"youtube_official": "YouTube 官方字幕",
                           "whisper_local": "本地语音识别"}.get(result["source"], result["source"])
            status_var.set(f"✅ 完成：{result['title']}（{source_name}，{result['segments']} 条）")
            messagebox.showinfo("完成",
                                f"已生成 {result['segments']} 条字幕（{source_name}）：\n"
                                + "\n".join(result["files"]))
        root.after(0, _done)

    def finish_fail(e):
        def _done():
            start_btn.configure(state="normal")
            status_var.set(f"❌ {e}")
            messagebox.showerror("失败", str(e))
        root.after(0, _done)

    def do_run():
        url = url_var.get().strip()
        browser = None if browser_var.get() == "不使用" else browser_var.get().lower()
        cookies = cookies_var.get().strip() or None
        try:
            result = run_youtube(
                url, Path(out_var.get()), lang=lang_var.get(), model=model_var.get(),
                mode=mode_var.get(), download_video_flag=download_video_var.get(),
                keep_audio=keep_audio_var.get(), cookies_file=cookies,
                cookies_from_browser=browser, log_fn=log_fn)
            finish_ok(result)
        except Exception as e:
            finish_fail(e)

    def run_in_thread(fn):
        start_btn.configure(state="disabled")
        threading.Thread(target=fn, daemon=True).start()

    def on_start():
        if not url_var.get().strip():
            messagebox.showwarning("缺链接", "先粘贴一个 YouTube 视频链接。")
            return
        run_in_thread(do_run)

    pad = {"padx": 8, "pady": 4}
    frm = ttk.Frame(root)
    frm.pack(fill="both", expand=True, padx=10, pady=10)

    ttk.Label(frm, text="YouTube 链接：").grid(row=0, column=0, sticky="w", **pad)
    ttk.Entry(frm, textvariable=url_var, width=70).grid(row=0, column=1, columnspan=2, sticky="ew", **pad)

    ttk.Label(frm, text="输出文件夹：").grid(row=1, column=0, sticky="w", **pad)
    ttk.Entry(frm, textvariable=out_var, width=55).grid(row=1, column=1, sticky="ew", **pad)
    ttk.Button(frm, text="选择…", command=browse_out).grid(row=1, column=2, **pad)

    ttk.Label(frm, text="字幕语言：").grid(row=2, column=0, sticky="w", **pad)
    ttk.Combobox(frm, textvariable=lang_var, values=["auto", "zh", "en"],
                 state="readonly", width=12).grid(row=2, column=1, sticky="w", **pad)
    ttk.Label(frm, text="auto=自动，zh=中文，en=英文").grid(row=2, column=2, sticky="w", **pad)

    ttk.Label(frm, text="识别模型：").grid(row=3, column=0, sticky="w", **pad)
    ttk.Combobox(frm, textvariable=model_var, values=["tiny", "base", "small", "medium"],
                 state="readonly", width=12).grid(row=3, column=1, sticky="w", **pad)
    ttk.Label(frm, text="tiny 最快，base 均衡，small 更准更慢").grid(row=3, column=2, sticky="w", **pad)

    ttk.Label(frm, text="模式：").grid(row=4, column=0, sticky="w", **pad)
    ttk.Combobox(frm, textvariable=mode_var,
                 values=["auto", "official", "whisper"],
                 state="readonly", width=12).grid(row=4, column=1, sticky="w", **pad)
    ttk.Label(frm, text="auto=优先官方字幕，没有再识别").grid(row=4, column=2, sticky="w", **pad)

    ttk.Label(frm, text="浏览器 Cookie：").grid(row=5, column=0, sticky="w", **pad)
    ttk.Combobox(frm, textvariable=browser_var,
                 values=["不使用", "chrome", "edge", "firefox", "brave"],
                 state="readonly", width=12).grid(row=5, column=1, sticky="w", **pad)
    ttk.Label(frm, text="被 YouTube 拦时用：先在浏览器登录 YouTube，选之前先把浏览器完全关掉").grid(row=5, column=2, sticky="w", **pad)

    ttk.Label(frm, text="cookies.txt：").grid(row=6, column=0, sticky="w", **pad)
    ttk.Entry(frm, textvariable=cookies_var, width=55).grid(row=6, column=1, sticky="ew", **pad)
    ttk.Button(frm, text="选择…", command=browse_cookies).grid(row=6, column=2, **pad)

    ttk.Checkbutton(frm, text="同时下载完整视频（默认不下，只下音频识别）",
                    variable=download_video_var).grid(row=7, column=0, columnspan=3, sticky="w", **pad)
    ttk.Checkbutton(frm, text="保留下载的音频 mp3（默认识别完删掉）",
                    variable=keep_audio_var).grid(row=8, column=0, columnspan=3, sticky="w", **pad)

    btn_row = ttk.Frame(frm)
    btn_row.grid(row=9, column=0, columnspan=3, sticky="ew", **pad)
    start_btn = ttk.Button(btn_row, text="▶  开始生成字幕", command=on_start)
    start_btn.pack(side="left", padx=4)
    ttk.Button(btn_row, text="转写本地音视频文件…", command=browse_local_file).pack(side="left", padx=4)
    ttk.Button(btn_row, text="打开输出文件夹",
               command=lambda: os.startfile(out_var.get()) if os.name == "nt" else None).pack(side="left", padx=4)

    log_box = scrolledtext.ScrolledText(frm, height=16, state="disabled", wrap="word")
    log_box.grid(row=10, column=0, columnspan=3, sticky="nsew", **pad)
    ttk.Label(frm, textvariable=status_var, wraplength=680).grid(row=11, column=0, columnspan=3, sticky="w", **pad)
    frm.columnconfigure(1, weight=1)
    frm.rowconfigure(10, weight=1)
    root.mainloop()


if __name__ == "__main__":
    # 有参数走命令行；没参数开图形界面
    if len(sys.argv) > 1:
        raise SystemExit(main_cli())
    else:
        run_gui()
