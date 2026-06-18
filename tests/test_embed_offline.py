# -*- coding: utf-8 -*-
"""离线验证 _render_rich 在图片可用时确实内嵌（monkeypatch 下载为本地图）。"""
import os, sys, struct, zlib, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pdf_builder
from pdf_builder import build_quiz_pdf


def _make_png(path, w=40, h=16):
    """生成一张极简纯色 PNG（无需 PIL）。"""
    def chunk(typ, data):
        c = typ + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # 8bit RGB
    raw = b""
    for _ in range(h):
        raw += b"\x00" + b"\x33\x66\x99" * w
    idat = zlib.compress(raw)
    with open(path, "wb") as f:
        f.write(sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


def main():
    img = os.path.join(tempfile.gettempdir(), "fake_formula.png")
    _make_png(img)
    # monkeypatch：任何 URL 都返回这张本地图
    pdf_builder._download_image = lambda url: img

    q = {
        "type": "single",
        "title": "设矩阵 【图片: https://x/a.png】 可逆，则下列正确的是",
        "options": ["A. 【图片: https://x/b.png】 成立", "B. 不成立", "C. 【图片: https://x/c.png】"],
        "answer": "A", "analysis": "", "source": "测试",
    }
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_embed_local.pdf")
    build_quiz_pdf([q], out, title="离线内嵌测试", course_name="线代", include_answers=True)
    size = os.path.getsize(out)
    print(f"PDF: {out}  {size} 字节")
    assert size > 3000
    print("OFFLINE EMBED TEST PASSED")


if __name__ == "__main__":
    main()
