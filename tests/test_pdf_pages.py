# -*- coding: utf-8 -*-
"""验证修复后 PDF 不再产生大量空白页（关闭自动分页冲突）。"""
import os, sys, struct, zlib, tempfile, glob
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pdf_builder
from pdf_builder import build_quiz_pdf


def _make_png(path, w=60, h=20):
    def chunk(t, d):
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)
    raw = b"".join(b"\x00" + b"\x33\x66\x99" * w for _ in range(h))
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def main():
    img = os.path.join(tempfile.gettempdir(), "fake_f.png")
    _make_png(img)
    pdf_builder._download_image = lambda url: img

    qs = []
    for i in range(30):
        qs.append({
            "type": "single",
            "title": f"第{i+1}题 设矩阵 【图片: https://x/a{i}.png】 满足条件，下列正确的是 【图片: https://x/b{i}.png】",
            "options": [f"A. 【图片: https://x/o{i}a.png】 成立",
                        "B. 不成立",
                        f"C. 【图片: https://x/o{i}c.png】",
                        "D. 以上都不对"],
            "answer": "A", "analysis": "", "source": "测试",
        })
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_pagecheck.pdf")
    build_quiz_pdf(qs, out, title="分页测试", course_name="线代", include_answers=True)

    import fitz
    doc = fitz.open(out)
    pages = doc.page_count
    # 统计空白页（正文文本极少的页）
    blank = 0
    for p in doc:
        t = p.get_text().strip()
        # 去掉页眉页脚后看是否还有正文
        body = t.replace("分页测试", "").replace("线代", "")
        body = "".join(c for c in body if not c.isdigit() and c not in "-– 　\n")
        if len(body) < 3:
            blank += 1
    print(f"题数=30, 总页数={pages}, 近空白页={blank}")
    doc.close()
    try:
        os.remove(out)
    except OSError:
        pass
    # 30题含图，合理应在 ~8-15 页内，且空白页应为 0
    assert pages <= 20, f"页数过多: {pages}"
    assert blank == 0, f"存在空白页: {blank}"
    print("PAGE LAYOUT TEST PASSED")


if __name__ == "__main__":
    main()
