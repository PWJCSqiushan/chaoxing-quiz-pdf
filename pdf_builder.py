# -*- coding: utf-8 -*-
"""
PDF 试卷生成模块（基于 fpdf2）。

输出结构（题目与答案分离，适合打印自测）：
  1. 封面（标题 / 课程 / 题量统计 / 生成时间）
  2. 题目区：按题型分组，留作答空间，不含答案
  3. 答案区：另起页，逐题列出正确答案与解析

中文字体：优先使用 fonts/ 目录下打包字体，其次系统字体。
"""
import os
import re
import io
import time
import hashlib
import tempfile
import threading
from typing import Dict, List, Optional

import requests
from fpdf import FPDF
from fpdf.enums import XPos, YPos

from api.logger import logger

# 公式/题图缓存目录与下载并发锁
_IMG_CACHE_DIR = os.path.join(tempfile.gettempdir(), "cx_quiz_imgs")
_IMG_LOCK = threading.Lock()
_IMG_MARKER = re.compile(r"【图片:\s*(.*?)】")
# 是否下载并内嵌公式图片（用户可关闭则降级为 [公式] 占位）
_EMBED_IMAGES = os.environ.get("PDF_EMBED_IMAGES", "1") == "1"

_TYPE_NAMES = {
    "single": "一、单选题",
    "multiple": "二、多选题",
    "judgement": "三、判断题",
    "completion": "四、填空题",
    "shortanswer": "五、简答题",
    "unknown": "六、其他",
}
_TYPE_ORDER = ["single", "multiple", "judgement", "completion", "shortanswer", "unknown"]

# 字体候选路径（按优先级）
_FONT_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "NotoSansSC-Regular.ttf"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "simhei.ttf"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "msyh.ttf"),
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/msyh.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
]


def _find_font() -> Optional[str]:
    """返回首个存在且能被 fpdf2 成功加载的字体路径。"""
    for p in _FONT_CANDIDATES:
        if not os.path.exists(p):
            continue
        try:
            # 用临时 FPDF 试加载，能加载才采用（过滤损坏/不兼容的 TTC）
            probe = FPDF()
            probe.add_font("probe", "", p)
            return p
        except Exception as e:
            logger.warning(f"字体 {p} 无法加载，跳过：{type(e).__name__}")
            continue
    return None


class QuizPDF(FPDF):
    def __init__(self, font_path: str, title: str):
        super().__init__(format="A4")
        self.doc_title = title
        self.set_auto_page_break(auto=True, margin=18)
        self.add_font("cjk", "", font_path)
        # fpdf2 对 TTC 也可加载，但部分 TTC 需指定；统一用同一文件做粗体回退
        try:
            self.add_font("cjk", "B", font_path)
        except Exception:
            pass
        self.set_margins(18, 16, 18)

    def header(self):
        if self.page_no() == 1:
            return
        self.set_font("cjk", "", 9)
        self.set_text_color(150, 150, 150)
        self.cell(0, 8, self.doc_title, align="C")
        self.ln(10)
        self.set_text_color(0, 0, 0)

    def footer(self):
        self.set_y(-12)
        self.set_font("cjk", "", 8)
        self.set_text_color(150, 150, 150)
        self.cell(0, 8, f"- {self.page_no()} -", align="C")
        self.set_text_color(0, 0, 0)


def _group_by_type(questions: List[Dict]) -> Dict[str, List[Dict]]:
    groups: Dict[str, List[Dict]] = {}
    for q in questions:
        groups.setdefault(q.get("type", "unknown"), []).append(q)
    return groups


def _safe(text: str) -> str:
    """fpdf2 unicode 模式下大多数字符可直接输出，这里仅做基础清洗。"""
    if text is None:
        return ""
    return str(text).replace("\r", "")


def _mc(pdf, h, text, **kwargs):
    """multi_cell 封装：始终回到左边距并换行，避免光标停在行尾导致宽度不足。"""
    kwargs.setdefault("new_x", XPos.LMARGIN)
    kwargs.setdefault("new_y", YPos.NEXT)
    pdf.multi_cell(0, h, _safe(text), **kwargs)


def build_quiz_pdf(
    questions: List[Dict],
    output_path: str,
    title: str = "超星自测试卷",
    course_name: str = "",
    include_answers: bool = True,
) -> str:
    """
    生成 PDF 文件，返回输出路径。

    questions      : 规范化题目列表（见 quiz_fetcher）。
    output_path    : 输出 PDF 路径。
    include_answers: 是否附答案区（题目区始终不含答案）。
    """
    font_path = _find_font()
    if not font_path:
        raise RuntimeError(
            "未找到可用的中文字体，请将 NotoSansSC-Regular.ttf 放入 fonts/ 目录，"
            "或在系统中安装中文字体。"
        )

    # 并发预下载所有公式图片，避免渲染时逐张串行下载拖慢速度
    if _EMBED_IMAGES:
        _prefetch_images(questions)

    pdf = QuizPDF(font_path, title)
    groups = _group_by_type(questions)
    total = len(questions)

    # ---------- 封面 ----------
    pdf.add_page()
    pdf.ln(40)
    pdf.set_font("cjk", "B", 24)
    _mc(pdf, 14, title, align="C")
    pdf.ln(6)
    if course_name:
        pdf.set_font("cjk", "", 14)
        _mc(pdf, 10, f"课程：{course_name}", align="C")
    pdf.ln(10)

    pdf.set_font("cjk", "", 12)
    stat_lines = [f"总题量：{total} 题"]
    for t in _TYPE_ORDER:
        if groups.get(t):
            name = _TYPE_NAMES[t].split("、")[-1]
            stat_lines.append(f"{name}：{len(groups[t])} 题")
    stat_lines.append("生成时间：" + time.strftime("%Y-%m-%d %H:%M"))
    for line in stat_lines:
        _mc(pdf, 9, line, align="C")

    pdf.ln(14)
    pdf.set_font("cjk", "", 10)
    pdf.set_text_color(120, 120, 120)
    _mc(pdf, 7, "说明：本试卷由超星题库自动抓取生成，仅供个人学习自测使用。", align="C")
    pdf.set_text_color(0, 0, 0)

    # ---------- 题目区 ----------
    pdf.add_page()
    qno = 0
    for t in _TYPE_ORDER:
        items = groups.get(t)
        if not items:
            continue
        pdf.set_font("cjk", "B", 14)
        pdf.ln(2)
        _mc(pdf, 10, _TYPE_NAMES[t] + f"（共 {len(items)} 题）")
        pdf.ln(1)
        for q in items:
            qno += 1
            _render_question(pdf, qno, q, with_answer=False)

    # ---------- 答案区 ----------
    if include_answers:
        pdf.add_page()
        pdf.set_font("cjk", "B", 16)
        _mc(pdf, 12, "参考答案与解析")
        pdf.ln(2)
        ano = 0
        for t in _TYPE_ORDER:
            items = groups.get(t)
            if not items:
                continue
            pdf.set_font("cjk", "B", 12)
            pdf.ln(1)
            _mc(pdf, 9, _TYPE_NAMES[t])
            for q in items:
                ano += 1
                _render_answer(pdf, ano, q)

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    pdf.output(output_path)
    logger.info(f"PDF 已生成: {output_path}（{total} 题）")
    return output_path


def _download_image(url: str) -> Optional[str]:
    """下载公式/题图到本地缓存，返回本地路径；失败返回 None。"""
    if not url:
        return None
    if url.startswith("//"):
        url = "https:" + url
    try:
        os.makedirs(_IMG_CACHE_DIR, exist_ok=True)
        ext = os.path.splitext(url.split("?")[0])[1].lower()
        if ext not in (".png", ".jpg", ".jpeg", ".gif", ".bmp"):
            ext = ".png"
        key = hashlib.md5(url.encode("utf-8")).hexdigest() + ext
        path = os.path.join(_IMG_CACHE_DIR, key)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return path
        with _IMG_LOCK:
            if os.path.exists(path) and os.path.getsize(path) > 0:
                return path
            headers = {
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"),
                "Referer": "https://mooc1.chaoxing.com/",
            }
            resp = requests.get(url, headers=headers, timeout=15)
            ctype = resp.headers.get("Content-Type", "")
            if resp.status_code == 200 and resp.content and "image" in ctype:
                with open(path, "wb") as f:
                    f.write(resp.content)
                return path
            logger.debug(f"下载图片非图片响应 {resp.status_code} {ctype} {url}")
    except Exception as e:
        logger.debug(f"下载图片失败 {url}: {type(e).__name__}")
    return None


def _prefetch_images(questions: List[Dict]):
    """并发预下载题目里所有公式图片到缓存，避免渲染时逐张串行下载。"""
    urls = set()
    for q in questions:
        for field in (q.get("title", ""), q.get("answer", ""), q.get("analysis", "")):
            for m in _IMG_MARKER.findall(field or ""):
                urls.add(m.strip())
        for opt in q.get("options", []) or []:
            for m in _IMG_MARKER.findall(opt or ""):
                urls.add(m.strip())
    urls = [u for u in urls if u]
    if not urls:
        return
    logger.info(f"预下载公式图片 {len(urls)} 张…")
    from concurrent.futures import ThreadPoolExecutor
    ok = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        for r in ex.map(_download_image, urls):
            if r:
                ok += 1
    logger.info(f"公式图片预下载完成：成功 {ok}/{len(urls)}")


def _img_size(path: str):
    """读取图片像素尺寸 (w, h)，失败返回 (2, 1)（横向估计，避免过宽）。"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:
        pass
    # 退而求其次：用 fpdf2 内置图片解析器
    try:
        from fpdf.image_parsing import get_img_info
        with open(path, "rb") as f:
            info = get_img_info(f.read())
        return (info["w"], info["h"])
    except Exception:
        return (2, 1)


def _render_rich(pdf: QuizPDF, h: float, text: str, indent: float = 0.0):
    """
    渲染含 【图片: URL】 标记的富文本：文字按字符排版（中文无空格需手动断行），
    图片下载后等比内嵌到约 1 行文字高度，行内插入；下载失败降级为 [公式]。
    """
    text = _safe(text)
    if not _IMG_MARKER.search(text):
        _mc(pdf, h, text)  # 纯文本，普通换行
        return

    left = pdf.l_margin + indent
    right = pdf.w - pdf.r_margin
    line_h = h
    img_h = max(3.0, h - 1.2)
    x = left
    y = pdf.get_y()

    def newline():
        nonlocal x, y
        x = left
        y += line_h
        # 接近页底则换页
        if y + line_h > pdf.h - pdf.b_margin:
            pdf.add_page()
            y = pdf.get_y()

    parts = _IMG_MARKER.split(text)  # 偶数=文字，奇数=URL
    for i, seg in enumerate(parts):
        if i % 2 == 0:
            for ch in seg:
                if ch in ("　",):
                    ch = " "
                w = pdf.get_string_width(ch) or 1
                if x + w > right:
                    newline()
                pdf.set_xy(x, y)
                pdf.cell(w, line_h, ch)
                x += w
        else:
            url = seg.strip()
            local = _download_image(url) if _EMBED_IMAGES else None
            drawn = False
            if local:
                try:
                    iw, ih = _img_size(local)
                    draw_h = img_h
                    draw_w = (iw / ih) * draw_h if ih else draw_h
                    draw_w = min(draw_w, right - left)
                    if x + draw_w > right:
                        newline()
                    pdf.image(local, x=x, y=y + 0.6, h=draw_h)
                    x += draw_w + 0.5
                    drawn = True
                except Exception as e:
                    logger.debug(f"内嵌图片失败: {type(e).__name__}")
            if not drawn:
                ph = "[公式]"
                w = pdf.get_string_width(ph)
                if x + w > right:
                    newline()
                pdf.set_xy(x, y)
                pdf.set_text_color(150, 150, 150)
                pdf.cell(w, line_h, ph)
                pdf.set_text_color(0, 0, 0)
                x += w
    pdf.set_xy(pdf.l_margin, y + line_h)


def _render_question(pdf: QuizPDF, no: int, q: Dict, with_answer: bool = False):
    pdf.set_font("cjk", "", 11)
    title = q.get("title", "")
    _render_rich(pdf, 7, f"{no}. {title}")
    # 选项
    for opt in q.get("options", []):
        _render_rich(pdf, 6.5, "    " + _safe(opt))
    # 作答区
    qtype = q.get("type")
    if qtype == "judgement":
        pdf.set_text_color(120, 120, 120)
        _mc(pdf, 6.5, "    （  ）对    （  ）错")
        pdf.set_text_color(0, 0, 0)
    elif qtype in ("completion", "shortanswer"):
        pdf.ln(2)
        pdf.set_draw_color(200, 200, 200)
        for _ in range(2 if qtype == "completion" else 4):
            y = pdf.get_y() + 5
            pdf.line(pdf.l_margin, y, pdf.w - pdf.r_margin, y)
            pdf.ln(8)
        pdf.set_draw_color(0, 0, 0)
    else:
        pdf.set_text_color(120, 120, 120)
        _mc(pdf, 6.5, "    答：")
        pdf.set_text_color(0, 0, 0)
    pdf.ln(2)


def _render_answer(pdf: QuizPDF, no: int, q: Dict):
    pdf.set_font("cjk", "", 11)
    ans = _safe(q.get("answer", "")) or "（题库未提供）"
    pdf.set_text_color(180, 30, 30)
    _mc(pdf, 7, f"{no}. 答案：{ans}")
    pdf.set_text_color(0, 0, 0)
    analysis = _safe(q.get("analysis", ""))
    if analysis:
        pdf.set_font("cjk", "", 9)
        pdf.set_text_color(90, 90, 90)
        _mc(pdf, 6, f"   解析：{analysis}")
        pdf.set_text_color(0, 0, 0)
    pdf.ln(1)
