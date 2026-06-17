# -*- coding: utf-8 -*-
"""加固改动的回归测试：去重指纹、密码哈希、凭证加密、PDF、AI 配置、模块导入。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_all_modules_import():
    import app  # noqa
    import quiz_fetcher  # noqa
    import pdf_builder  # noqa
    import ai_explainer  # noqa
    import browser_fetcher  # noqa
    from api import base, database, decode  # noqa


def test_fingerprint_order_independent():
    """同一题选项乱序、字母标号不同，应得到相同指纹（H4）。"""
    from quiz_fetcher import question_fingerprint
    q1 = {"type": "single", "title": "1+1=?", "options": ["A. 一", "B. 二", "C. 三"]}
    q2 = {"type": "single", "title": "1+1=?", "options": ["C. 三", "A. 一", "B. 二"]}  # 乱序
    q3 = {"type": "single", "title": "1+1=?", "options": ["X. 一", "Y. 二", "Z. 三"]}  # 不同标号
    assert question_fingerprint(q1) == question_fingerprint(q2)
    assert question_fingerprint(q1) == question_fingerprint(q3)
    # 不同题应不同
    q4 = {"type": "single", "title": "2+2=?", "options": ["A. 三", "B. 四"]}
    assert question_fingerprint(q1) != question_fingerprint(q4)


def test_password_hash_and_verify():
    """PBKDF2 新格式哈希 + 恒定时间校验 + 旧格式兼容（H3/L1）。"""
    from api.database import _hash_password, _verify_password, _legacy_hash, _needs_rehash
    h = _hash_password("Secret123")
    assert h.startswith("pbkdf2$")
    assert _verify_password("Secret123", h)
    assert not _verify_password("wrong", h)
    # 每次盐随机 → 同密码哈希不同
    assert _hash_password("Secret123") != h
    # 旧格式可校验且标记需升级
    legacy = _legacy_hash("OldPass")
    assert _verify_password("OldPass", legacy)
    assert _needs_rehash(legacy)
    assert not _needs_rehash(h)


def test_secret_encryption_roundtrip():
    """超星密码加密存储可逆，且密文不含明文（H2）。"""
    from api.database import _encrypt_secret, _decrypt_secret
    plain = "my-cx-password-138"
    token = _encrypt_secret(plain)
    assert token != plain
    assert plain not in token
    assert token.startswith("enc")
    assert _decrypt_secret(token) == plain
    # 历史明文（无前缀）应原样返回
    assert _decrypt_secret(plain) == plain


def test_pdf_generation():
    """PDF 生成不崩，且选项含 None 不抛 TypeError（L11）。"""
    from pdf_builder import build_quiz_pdf
    qs = [
        {"type": "single", "title": "测试题 emoji 😀 行列式", "options": ["A. 选项一", None, "C. 选项三"],
         "answer": "A", "analysis": "因为A"},
        {"type": "judgement", "title": "判断题", "options": [], "answer": "对", "analysis": ""},
    ]
    out = os.path.join(tempfile.gettempdir(), "test_harden.pdf")
    build_quiz_pdf(qs, out, title="加固测试", course_name="测试课", include_answers=True)
    assert os.path.exists(out) and os.path.getsize(out) > 1000
    os.remove(out)


def test_ai_from_config_timeout():
    """from_config 应读取 timeout（L12）。"""
    from ai_explainer import AIExplainer
    a = AIExplainer.from_config({"api_key": "k", "preset": "deepseek", "timeout": 99})
    assert a.timeout == 99
    b = AIExplainer.from_config({"api_key": "k", "preset": "deepseek"})
    assert b.timeout == 40


if __name__ == "__main__":
    test_all_modules_import()
    test_fingerprint_order_independent()
    test_password_hash_and_verify()
    test_secret_encryption_roundtrip()
    test_pdf_generation()
    test_ai_from_config_timeout()
    print("ALL HARDENING TESTS PASSED")
