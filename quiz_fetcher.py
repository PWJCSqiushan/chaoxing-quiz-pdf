# -*- coding: utf-8 -*-
"""
抓题模块。

提供两种抓题策略：
  模式A (single)   ：抓取指定章节内已存在的测验/自测卷题目（一次性）。
  模式B (accumulate)：对指定课程的所有章节测验反复抓取，按题干指纹去重累积，
                      逐步逼近完整题库。

题目统一规范为如下结构，供 PDF 生成使用：
  {
    "id": str,
    "type": "single|multiple|judgement|completion|shortanswer|unknown",
    "title": str,
    "options": list[str],     # 形如 ["A. xxx", "B. yyy"]
    "answer": str,            # 正确答案（可能为空）
    "analysis": str,          # 解析（可能为空）
    "source": str,            # 来源章节/测验名
  }
"""
import hashlib
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, List, Optional

from api.base import Chaoxing
from api.logger import logger


def _normalize(text: str) -> str:
    """题干归一化，用于去重指纹：去标点、空白、图片URL中的变量参数。"""
    if not text:
        return ""
    text = re.sub(r"【图片:.*?】", "【图片】", text)
    text = re.sub(r"[\s　]+", "", text)
    text = re.sub(r"[（）()【】\[\]．.。，,、；;：:？?！!\"'“”‘’]", "", text)
    return text.lower()


def question_fingerprint(q: Dict) -> str:
    """根据题型+题干+选项生成稳定指纹。"""
    basis = q.get("type", "") + _normalize(q.get("title", ""))
    opts = "".join(_normalize(o) for o in q.get("options", []))
    basis += opts
    return hashlib.md5(basis.encode("utf-8")).hexdigest()


class QuizFetcher:
    def __init__(self, chaoxing: Chaoxing, progress_cb: Optional[Callable[[str], None]] = None):
        self.cx = chaoxing
        self._progress_cb = progress_cb
        self._lock = threading.Lock()

    def _emit(self, msg: str):
        logger.info(msg)
        if self._progress_cb:
            try:
                self._progress_cb(msg)
            except Exception:
                pass

    # ---------------- 通用：解析一份测验为规范题目 ----------------

    def _parse_work(self, course: Dict, job: Dict, job_info: Dict) -> List[Dict]:
        parsed = self.cx.fetch_work_questions(course, job, job_info)
        if not parsed:
            return []
        source = parsed.get("_work_title") or job.get("title", "章节测验")
        result = []
        for q in parsed.get("questions", []):
            result.append({
                "id": q.get("id", ""),
                "type": q.get("type", "unknown"),
                "title": q.get("title", ""),
                "options": q.get("options", []),
                "answer": q.get("answer", ""),
                "analysis": q.get("analysis", ""),
                "source": source,
            })
        return result

    # ---------------- 收集课程内全部 workid 任务 ----------------

    def _collect_work_jobs(self, course: Dict) -> List[Dict]:
        """遍历课程所有章节，收集 (job, job_info) 列表。"""
        self._emit(f"读取课程章节: {course.get('title', '')}")
        point_data = self.cx.get_course_point(course["courseId"], course["clazzId"], course["cpi"])
        points = point_data.get("points", [])
        self._emit(f"共 {len(points)} 个章节，开始扫描测验任务…")

        jobs: List[Dict] = []
        for i, point in enumerate(points, 1):
            try:
                job_list, job_info = self.cx.get_job_list(course, point)
            except Exception as e:
                logger.debug(f"章节 {point.get('title')} 任务读取失败: {e}")
                continue
            for job in job_list:
                job["_point_title"] = point.get("title", "")
                if not job.get("title"):
                    job["title"] = point.get("title", "章节测验")
                jobs.append({"job": job, "job_info": job_info})
            if i % 5 == 0:
                self._emit(f"已扫描 {i}/{len(points)} 章节，发现 {len(jobs)} 个测验")
        self._emit(f"扫描完成，共发现 {len(jobs)} 个测验任务")
        return jobs

    # ---------------- 模式A：单章节 / 指定测验 ----------------

    def fetch_single_chapter(self, course: Dict, point: Dict) -> List[Dict]:
        """抓取指定章节内的所有测验题目。"""
        job_list, job_info = self.cx.get_job_list(course, point)
        if not job_list:
            self._emit("该章节未发现测验任务")
            return []
        all_q: List[Dict] = []
        for item in job_list:
            qs = self._parse_work(course, item, job_info)
            all_q.extend(qs)
            self._emit(f"测验《{item.get('title')}》抓到 {len(qs)} 题")
        return self._dedup(all_q)

    # ---------------- 模式B：全课程累积去重 ----------------

    def fetch_course_accumulate(
        self,
        course: Dict,
        rounds: int = 3,
        concurrency: int = 3,
        target: int = 0,
    ) -> List[Dict]:
        """
        对整个课程反复抓取测验，去重累积题目。

        rounds      ：最多重复抓取轮数（自测每次抽题不同，多轮可覆盖更多）。
        concurrency ：同一轮内并发抓取的测验数量。
        target      ：目标题量，达到即提前结束（0 表示不设上限）。
        """
        jobs = self._collect_work_jobs(course)
        if not jobs:
            self._emit("未发现任何可抓取的测验任务")
            return []

        collected: Dict[str, Dict] = {}

        for r in range(1, rounds + 1):
            self._emit(f"===== 第 {r}/{rounds} 轮抓取 =====")
            before = len(collected)

            def worker(item):
                return self._parse_work(course, item["job"], item["job_info"])

            with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
                futures = {ex.submit(worker, it): it for it in jobs}
                for fut in as_completed(futures):
                    try:
                        qs = fut.result()
                    except Exception as e:
                        logger.debug(f"抓题线程异常: {e}")
                        continue
                    with self._lock:
                        for q in qs:
                            fp = question_fingerprint(q)
                            if fp not in collected:
                                collected[fp] = q
                    if target and len(collected) >= target:
                        break

            gained = len(collected) - before
            self._emit(f"第 {r} 轮新增 {gained} 题，累计 {len(collected)} 题")

            if target and len(collected) >= target:
                self._emit(f"已达到目标题量 {target}，提前结束")
                break
            if gained == 0 and r >= 2:
                self._emit("连续无新增题目，提前结束")
                break
            time.sleep(0.5)

        return self._sort(list(collected.values()))

    # ---------------- 工具 ----------------

    def _dedup(self, questions: List[Dict]) -> List[Dict]:
        seen = {}
        for q in questions:
            seen.setdefault(question_fingerprint(q), q)
        return self._sort(list(seen.values()))

    @staticmethod
    def _sort(questions: List[Dict]) -> List[Dict]:
        order = {"single": 0, "multiple": 1, "judgement": 2, "completion": 3, "shortanswer": 4, "unknown": 5}
        return sorted(questions, key=lambda q: order.get(q.get("type", "unknown"), 9))
