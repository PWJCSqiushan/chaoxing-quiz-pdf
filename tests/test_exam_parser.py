# -*- coding: utf-8 -*-
"""考试/自测答题页解析器的回归与新结构测试。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.decode import (  # noqa: E402
    decode_questions_info,
    _infer_type_from_text,
    _strip_type_prefix,
)

HOMEWORK_HTML = """
<form>
  <div class="singleQuesId" data="123">
    <div class="TiMu" data="0">
      <div class="Zy_TItle"><span>1+1=?</span></div>
      <ul>
        <li aria-label="A. 2">A</li>
        <li aria-label="B. 3">B</li>
      </ul>
    </div>
  </div>
</form>
"""

EXAM_HTML = """
<html><body>
  <div class="questionLi" data="0">
    <div class="mark_name">1.（单选题）行列式的值为多少？</div>
    <ul class="mark_letter"><li>A. 0</li><li>B. 1</li><li>C. 2</li></ul>
  </div>
  <div class="questionLi" data="3">
    <div class="mark_name">2.（判断题）矩阵可逆当且仅当行列式非零。</div>
    <ul class="mark_letter"><li>A. 对</li><li>B. 错</li></ul>
  </div>
</body></html>
"""


def test_homework_still_parses():
    r = decode_questions_info(HOMEWORK_HTML)
    assert len(r["questions"]) == 1
    assert "1+1" in r["questions"][0]["title"]


def test_exam_structure_parses():
    r = decode_questions_info(EXAM_HTML)
    qs = r["questions"]
    assert len(qs) == 2, f"expected 2, got {len(qs)}"
    assert qs[0]["type"] == "single"
    assert "行列式的值" in qs[0]["title"]
    assert len(qs[0]["options"]) == 3
    assert qs[1]["type"] == "judgement"


def test_infer_and_strip():
    assert _infer_type_from_text("（多选题）以下") == "multiple"
    assert _strip_type_prefix("1.（单选题）行列式") == "行列式"


if __name__ == "__main__":
    test_homework_still_parses()
    test_exam_structure_parses()
    test_infer_and_strip()
    print("ALL TESTS PASSED")
    # 展示解析细节
    for q in decode_questions_info(EXAM_HTML)["questions"]:
        print(f"  type={q['type']} | title={q['title']} | opts={q['options']}")
