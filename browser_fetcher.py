# -*- coding: utf-8 -*-
"""
浏览器半自动抓题（Playwright）。

超星「自测」答题页被滑块/拼图验证码挡住，纯 requests 无法获取题目。
本模块用一个真实 Chromium：
  1. 注入已登录的超星 cookie（复用 requests 会话，免再登录）；
  2. 用 requests 端创建自测卷、组卷、定位入口（这几步不被验证码拦）；
  3. 在浏览器里打开答题页流程，遇到拖拽验证码时由用户手动拖一下；
  4. 题目渲染出来后，从页面 DOM 直接提取题目与（若有）答案。

“半自动”：仅验证码那一下需要人工，其余全自动��
"""
import os
import re
import time
from typing import Callable, Dict, List, Optional

from api.base import Chaoxing
from api.decode import decode_questions_info
from api.logger import logger
from quiz_fetcher import question_fingerprint, QuizFetcher

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")

# 答题页 DOM 里题目可能出现的容器特征（命中其一即认为题目已渲染）
QUESTION_MARKERS = ["singleQuesId", "TiMu", "questionLi", "Cy_ulTk", "mark_name",
                    "queTitle", "mark_letter", "qtContent", "timubox"]


class BrowserGrabber:
    def __init__(self, chaoxing: Chaoxing, progress_cb: Optional[Callable[[str], None]] = None,
                 headless: bool = False):
        self.cx = chaoxing
        self._cb = progress_cb
        self.headless = headless
        self._dumped_sample = False

    def _emit(self, msg: str):
        logger.info(msg)
        if self._cb:
            try:
                self._cb(msg)
            except Exception:
                pass

    def _cookies_for_playwright(self) -> List[Dict]:
        """把 requests 会话里的 cookie 转成 Playwright 格式。"""
        out = []
        for c in self.cx.session.cookies:
            out.append({
                "name": c.name,
                "value": c.value,
                "domain": c.domain if c.domain else ".chaoxing.com",
                "path": c.path or "/",
            })
        return out

    def grab(
        self,
        course: Dict,
        count: int = 50,
        papers: int = 5,
        target: int = 0,
        per_paper_timeout: int = 240,
    ) -> List[Dict]:
        """
        浏览器半自动抓题主流程。

        count   ：每份自测卷抽题数。
        papers  ：最多新建几份自测卷。
        target  ：目标题量（0=抓到无新增为止）。
        per_paper_timeout：每份卷子等待用户过验证码+渲染题目的最长秒数。
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self._emit("未安装 Playwright，请先执行: pip install playwright 并 python -m playwright install chromium")
            return []

        meta = self.cx.get_selftest_meta(course)
        bank = self.cx.selftest_question_count(course)
        if bank < 0:
            self._emit("无法读取题库（超星会话可能已失效，请退出后重新登录超星账号）")
        elif bank > 0:
            self._emit(f"题库可抽题量约 {bank} 题")
            if not target:
                target = bank

        collected: Dict[str, Dict] = {}
        empty_streak = 0

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            context = browser.new_context()
            try:
                try:
                    context.add_cookies(self._cookies_for_playwright())
                except Exception as e:
                    logger.debug(f"注入 cookie 失败: {e}")
                page = context.new_page()

                for r in range(1, papers + 1):
                    self._emit(f"第 {r}/{papers} 份自测卷（抽 {count} 题）：创建中…")
                    # —— 用 requests 创建并定位（不被验证码拦）——
                    task_id = self.cx.create_selftest(course, meta, count=count)
                    if not task_id:
                        self._emit("新建自测失败，跳过")
                        empty_streak += 1
                        if empty_streak >= 2:
                            break
                        continue
                    paper_id = self.cx.poll_selftest_paper(course, task_id)
                    if not paper_id:
                        self._emit("组卷失败，跳过")
                        continue
                    entry = self.cx.find_paper_entry(course, paper_id, meta)
                    if not entry:
                        self._emit("未定位到自测卷入口，跳过")
                        continue

                    # —— 浏览器里打开答题流程 ——
                    html = self._open_and_extract(context, page, course, entry, per_paper_timeout)
                    if not html:
                        self._emit("本份未取到题目页（超时或被关闭）")
                        empty_streak += 1
                        if empty_streak >= 2:
                            self._emit("连续失败，停止")
                            break
                        continue

                    before = len(collected)
                    qs = self._parse(html, paper_id)
                    for q in qs:
                        q["source"] = f"{course.get('title','')} 自测"
                        fp = question_fingerprint(q)
                        if fp not in collected:
                            collected[fp] = q
                    gained = len(collected) - before
                    self._emit(f"第 {r} 份解析到 {len(qs)} 题，新增 {gained}，累计 {len(collected)} 题")

                    if target and len(collected) >= target:
                        self._emit(f"已覆盖目标题量 {target}，结束")
                        break
                    empty_streak = 0 if gained else empty_streak + 1
                    if empty_streak >= 2 and r >= 2:
                        self._emit("连续无新增，结束")
                        break
            finally:
                # 异常路径也确保上下文与浏览器被关闭，回收资源
                try:
                    context.close()
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass

        return QuizFetcher._sort(list(collected.values()))

    def _open_and_extract(self, context, page, course: Dict, entry: Dict,
                          timeout: int) -> Optional[str]:
        """
        在浏览器打开 examnotes → 进入考试 → (用户过验证码) → 答题页，提取题目 HTML。
        """
        examnotes = (
            f"{self.cx.EXAM_HOST}/exam/test/examcode/examnotes"
            f"?courseId={course['courseId']}&classId={course['clazzId']}"
            f"&examId={entry['tId']}&cpi={course['cpi']}"
        )

        self._emit("→ 浏览器已打开答题须知页，请勾选同意并点【进入考试】，遇到拖拽验证码请手动完成")
        try:
            page.goto(examnotes, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            logger.debug(f"打开 examnotes 失败: {e}")

        # 尽量自动勾选“已阅读”复选框并点“进入考试”
        for sel in ["#readCheck", ".read_check", "input[type=checkbox]"]:
            try:
                if page.locator(sel).count() > 0:
                    page.locator(sel).first.click(timeout=2000)
                    break
            except Exception:
                pass
        # “进入考试”按钮可能开新标签页；只点击一次，避免同页跳转时重复点击
        answer_page = page
        clicked = False
        try:
            with context.expect_page(timeout=5000) as pop:
                self._click_enter(page)
                clicked = True
            answer_page = pop.value
        except Exception:
            # 没有新开标签：若上面因点击前就异常未点到，则补点一次（同页跳转）
            if not clicked:
                self._click_enter(page)
            answer_page = self._latest_question_page(context) or page

        # 轮询等待题目渲染（用户此时在过验证码）
        deadline = time.time() + timeout
        while time.time() < deadline:
            ap = self._latest_question_page(context) or answer_page
            # DOM 级判定：真正存在题目容器元素，且容器内有���本，才算渲染完成
            rendered = False
            try:
                loc = ap.locator(
                    "div.singleQuesId, div.questionLi, div.TiMu, div.timu, div.Cy_ulTk"
                )
                if loc.count() > 0:
                    txt = (loc.first.inner_text(timeout=1500) or "").strip()
                    rendered = len(txt) > 0
            except Exception:
                rendered = False
            if rendered:
                self._emit("✓ 检测到题目已渲染，正在提取…")
                time.sleep(1.0)
                try:
                    return ap.content()
                except Exception:
                    return None
            time.sleep(2.0)
        return None

    def _click_enter(self, page):
        for sel in ["text=进入考试", "a:has-text('进入考试')", "#startExamId", ".jb_btn_92",
                    "button:has-text('进入考试')"]:
            try:
                if page.locator(sel).count() > 0:
                    page.locator(sel).first.click(timeout=2000)
                    return
            except Exception:
                pass

    def _latest_question_page(self, context):
        """在所有打开的标签里找出答题页（排除考试须知页 examnotes）。"""
        for pg in reversed(context.pages):
            try:
                url = pg.url or ""
                if "examnotes" in url:
                    continue
                if any(k in url for k in ["reVersionTestStart", "lookPaper"]) or "/exam/test/" in url:
                    return pg
            except Exception:
                continue
        return None

    def _parse(self, html: str, paper_id) -> List[Dict]:
        """解析答题页 HTML。先用现成解析器，失败则落盘原始 HTML 供校准。"""
        # 首份卷子无论成功与否，都留存一份原始 HTML，便于核对真实结构
        if not self._dumped_sample:
            self._dump_html(html, f"sample_answerpage_{paper_id}.html")
            self._dumped_sample = True

        parsed = decode_questions_info(html)
        qs = parsed.get("questions", [])
        if qs:
            return [{
                "id": q.get("id", ""), "type": q.get("type", "unknown"),
                "title": q.get("title", ""), "options": q.get("options", []),
                "answer": q.get("answer", ""), "analysis": q.get("analysis", ""),
            } for q in qs]
        # 解析为空：落盘（带 debug 前缀，醒目）
        self._dump_html(html, f"debug_answerpage_{paper_id}.html")
        self._emit(f"题目页结构未匹配，已保存到 output/debug_answerpage_{paper_id}.html（请反馈给开发者校准）")
        return []

    def _dump_html(self, html: str, filename: str):
        if not html:
            return
        try:
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            with open(os.path.join(OUTPUT_DIR, filename), "w", encoding="utf-8") as f:
                f.write(html)
            logger.info(f"已保存答题页 HTML: output/{filename}（{len(html)} 字符）")
        except Exception as e:
            logger.debug(f"保存 HTML 失败: {e}")
