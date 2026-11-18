#!/usr/bin/env python3
"""What Muse sends the user once Acost is installed: the intro text and the short video.

  python3 ~/acost/tools/welcome.py

Prints one JSON line:
  ok, message (the intro, to send as it is), video (absolute path of the MP4 to attach),
  video_url (the same video online, for when a local file cannot be attached), caption, next.
Reads WELCOME.md (the text between the two --- lines) and checks the video is on this machine.
"""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
VIDEO = ROOT / "assets" / "pin_tab.mp4"
VIDEO_URL = "https://raw.githubusercontent.com/Uanqaoh/acost-app/main/assets/pin_tab.mp4"
CAPTION = "把「Acost 看板」拖到底部栏，以后一点就能看账：☰ → 按住 Acost 看板 → 拖到 Main chat 下面。"


def intro(text):
    """The part of WELCOME.md between the first and the last line that is only ---."""
    lines = text.splitlines()
    marks = [i for i, line in enumerate(lines) if line.strip() == "---"]
    if len(marks) < 2:
        raise ValueError("WELCOME.md 里找不到两条 --- 分割线")
    return "\n".join(lines[marks[0] + 1:marks[-1]]).strip()


def main():
    try:
        message = intro((ROOT / "WELCOME.md").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error),
                          "next": "运行 python3 ~/acost/tools/fetch.py 更新程序，再运行一次这条命令。"}, ensure_ascii=False))
        return 1
    have_video = VIDEO.is_file()
    print(json.dumps({
        "ok": True, "message": message, "video": str(VIDEO) if have_video else None,
        "video_url": VIDEO_URL, "caption": CAPTION,
        "next": ("先把 message 原样发给用户；再把 video 这个 MP4 作为视频附件发给用户，配上 caption。"
                 "两样都要发，视频不能省。附件发不出去时，改发 video_url 这个链接，配上 caption。"
                 if have_video else
                 "本机没有视频文件：先运行 python3 ~/acost/tools/fetch.py，再运行一次这条命令。"
                 "还是没有的话，把 message 发给用户，再发 video_url 这个链接，配上 caption。"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
