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
import time
from typing import Dict, List, Optional

from fpdf import FPDF
from fpdf.enums import XPos, YPos

from api.logger import logger

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
    for p in _FONT_CANDIDATES:
        if os.path.exists(p):
            return p
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


def _render_question(pdf: QuizPDF, no: int, q: Dict, with_answer: bool = False):
    pdf.set_font("cjk", "", 11)
    title = _safe(q.get("title", ""))
    _mc(pdf, 7, f"{no}. {title}")
    # 选项
    for opt in q.get("options", []):
        _mc(pdf, 6.5, "    " + opt)
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
