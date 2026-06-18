# -*- coding: utf-8 -*-
"""用真实保存的答题页样本验证 decode_exam_page。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.decode import decode_exam_page, exam_page_total

SAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "output", "sample_answerpage_542995033.html")


def main():
    if not os.path.exists(SAMPLE):
        print("样本不存在，跳过：", SAMPLE)
        return
    html = open(SAMPLE, encoding="utf-8").read()
    total = exam_page_total(html)
    print("答题卡总题数:", total)
    res = decode_exam_page(html)
    print("paper_id:", res["paper_id"], "test_paper_id:", res["test_paper_id"], "total:", res["total"])
    qs = res["questions"]
    print("解析到题数:", len(qs))
    for q in qs:
        print("  id:", q["id"], "| type:", q["type"])
        print("  title:", q["title"][:120])
        for o in q["options"]:
            print("     opt:", o[:100])
    # 断言：至少解析出 1 题、含图片标记、有 4 个选项
    assert total >= 40, f"总题数异常: {total}"
    assert len(qs) == 1
    q = qs[0]
    assert q["type"] == "single", q["type"]
    assert "【图片:" in q["title"], "题干应保留图片标记"
    assert len(q["options"]) == 4, f"选项数={len(q['options'])}"
    assert all("【图片:" in o for o in q["options"]), "选项应含图片标记"
    print("\nSAMPLE PARSE TEST PASSED")


if __name__ == "__main__":
    main()
