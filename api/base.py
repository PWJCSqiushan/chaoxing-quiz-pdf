# -*- coding: utf-8 -*-
"""
超星学习通接口封装（抽题专用）。

与 chaoxing-fanya 不同，这里每个 Chaoxing 实例持有独立 requests.Session，
以支持 Web 服务下的多用户并发，互不串号。
"""
import functools
import random
import re
import threading
import time
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter

from api.cipher import AESCipher
from api.config import GlobalConst as gc
from api.cookies import save_cookies, load_cookies, cookies_path_for
from api.decode import (
    decode_course_list,
    decode_course_folder,
    decode_course_point,
    decode_course_card,
    decode_questions_info,
)
from api.logger import logger


def get_timestamp() -> str:
    return str(int(time.time() * 1000))


class Account:
    def __init__(self, username: str = "", password: str = ""):
        self.username = username
        self.password = password


class RateLimiter:
    def __init__(self, call_interval: float):
        self.last_call = time.time()
        self.lock = threading.Lock()
        self.call_interval = call_interval

    def limit_rate(self, random_time: bool = False, random_min: float = 0.0, random_max: float = 1.0):
        with self.lock:
            if random_time:
                time.sleep(random.uniform(random_min, random_max))
            now = time.time()
            elapsed = now - self.last_call
            if elapsed <= self.call_interval:
                time.sleep(self.call_interval - elapsed)
            self.last_call = time.time()


class Chaoxing:
    def __init__(self, account: Optional[Account] = None, session_key: Optional[str] = None):
        self.account = account or Account()
        self.cipher = AESCipher()
        self.rate_limiter = RateLimiter(0.3)
        # session_key 用于 cookie 文件隔离，通常传本地用户ID
        self.session_key = session_key or (account.username if account else "default")
        self._cookies_path = cookies_path_for(str(self.session_key))

        self.session = requests.Session()
        self.session.mount("https://", HTTPAdapter(max_retries=5))
        self.session.mount("http://", HTTPAdapter(max_retries=5))
        self.session.request = functools.partial(self.session.request, timeout=15)
        self.session.headers.update(gc.HEADERS)
        # 载入已有 cookie
        saved = load_cookies(self._cookies_path)
        if saved:
            self.session.cookies.update(saved)

    # ---------------- 登录 ----------------

    def login(self, login_with_cookies: bool = False) -> Dict:
        if login_with_cookies:
            if self._validate_session():
                logger.info("Cookie 登录成功")
                return {"status": True, "msg": "登录成功"}
            if self.account.username and self.account.password:
                return self.login(login_with_cookies=False)
            return {"status": False, "msg": "cookies 已失效，请重新输入账号密码"}

        _url = "https://passport2.chaoxing.com/fanyalogin"
        _data = {
            "fid": "-1",
            "uname": self.cipher.encrypt(self.account.username),
            "password": self.cipher.encrypt(self.account.password),
            "refer": "https%3A%2F%2Fi.chaoxing.com",
            "t": True,
            "forbidotherlogin": 0,
            "validate": "",
            "doubleFactorLogin": 0,
            "independentId": 0,
        }
        try:
            resp = self.session.post(_url, headers=gc.HEADERS, data=_data)
            data = resp.json()
        except Exception as e:
            return {"status": False, "msg": f"登录请求失败: {e}"}

        if data.get("status") is True:
            save_cookies(self.session, self._cookies_path)
            logger.info("账号密码登录成功")
            return {"status": True, "msg": "登录成功"}
        return {"status": False, "msg": str(data.get("msg2") or data.get("msg") or "登录失败")}

    def _validate_session(self) -> bool:
        if not self.session.cookies.get("_uid"):
            return False
        try:
            resp = self.session.post(
                "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/courselistdata",
                data={"courseType": 1, "courseFolderId": 0, "query": "", "superstarClass": 0},
                timeout=10,
            )
        except requests.RequestException:
            return False
        if resp.status_code != 200:
            return False
        if "passport2.chaoxing.com" in resp.text or "/login" in resp.text.lower():
            return False
        return True

    def get_uid(self) -> Optional[str]:
        return self.session.cookies.get("_uid") or self.session.cookies.get("UID")

    def get_fid(self) -> Optional[str]:
        return self.session.cookies.get("fid")

    # ---------------- 课程 / 章节 / 任务 ----------------

    def get_course_list(self) -> List[Dict]:
        _url = "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/courselistdata"
        _data = {"courseType": 1, "courseFolderId": 0, "query": "", "superstarClass": 0}
        _headers = {
            "Referer": "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/interaction?moocDomain=https://mooc1-1.chaoxing.com/mooc-ans",
        }
        resp = self.session.post(_url, headers=_headers, data=_data)
        course_list = decode_course_list(resp.text)

        # 课程文件夹
        try:
            interaction = self.session.get("https://mooc2-ans.chaoxing.com/mooc2-ans/visit/interaction")
            for folder in decode_course_folder(interaction.text):
                fr = self.session.post(_url, data={
                    "courseType": 1, "courseFolderId": folder["id"], "query": "", "superstarClass": 0,
                })
                course_list += decode_course_list(fr.text)
        except Exception as e:
            logger.debug(f"读取课程文件夹失败: {e}")

        # 去重
        seen = set()
        uniq = []
        for c in course_list:
            if c["courseId"] in seen:
                continue
            seen.add(c["courseId"])
            uniq.append(c)
        return uniq

    def get_course_meta(self, course: Dict) -> Dict:
        """
        访问课程学习主页（mycourse/stu），解析自测/考试所需的 enc 等隐藏字段。

        该页面（即用户提供的源码）含有 examEnc / workEnc / enc / openc / fid 等关键参数，
        自测模块的接口通常需要它们。
        """
        url = (
            "https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/stu"
            f"?courseid={course['courseId']}&clazzid={course['clazzId']}"
            f"&cpi={course['cpi']}&ut=s&pageHeader=5&v=2"
        )
        meta: Dict[str, str] = {}
        try:
            resp = self.session.get(url)
            soup = BeautifulSoup(resp.text, "lxml")
            for key in ("enc", "openc", "oldenc", "workEnc", "examEnc", "fid",
                        "cfid", "bbsid", "courseBelongSchoolId"):
                el = soup.find("input", id=key) or soup.find("input", attrs={"name": key})
                if el and el.get("value"):
                    meta[key] = el["value"]
        except Exception as e:
            logger.debug(f"解析课程 meta 失败: {e}")
        return meta

    def get_course_point(self, courseid: str, clazzid: str, cpi: str) -> Dict:
        _url = (
            f"https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/studentcourse"
            f"?courseid={courseid}&clazzid={clazzid}&cpi={cpi}&ut=s"
        )
        resp = self.session.get(_url)
        return decode_course_point(resp.text)

    def get_job_list(self, course: Dict, point: Dict) -> Tuple[List[Dict], Dict]:
        """获取章节内的所有任务点，返回 (workid任务列表, job_info)。"""
        self.rate_limiter.limit_rate()
        job_list: List[Dict] = []
        job_info: Dict = {}
        cards_params = {
            "clazzid": course["clazzId"],
            "courseid": course["courseId"],
            "knowledgeid": point["id"],
            "ut": "s",
            "cpi": course["cpi"],
            "v": "2025-0424-1038-3",
            "mooc2": 1,
        }
        for num in "0123456":
            cards_params["num"] = num
            resp = self.session.get(
                "https://mooc1.chaoxing.com/mooc-ans/knowledge/cards", params=cards_params
            )
            if resp.status_code != 200:
                logger.debug(f"获取任务点卡片失败: {resp.status_code}")
                break
            _jobs, _info = decode_course_card(resp.text)
            if _info.get("notOpen", False):
                return [], _info
            job_list += _jobs
            if _info:
                job_info.update(_info)
        return job_list, job_info

    # ---------------- 抓题 ----------------

    def fetch_work_questions(self, course: Dict, job: Dict, job_info: Dict) -> Optional[Dict]:
        """
        拉取单个测验/作业的题目页并解析。
        返回 decode_questions_info 的结果（含 questions 列表），失败返回 None。
        """
        _url = "https://mooc1.chaoxing.com/mooc-ans/api/work"
        params = {
            "api": "1",
            "workId": job["jobid"].replace("work-", ""),
            "jobid": job["jobid"],
            "originJobId": job["jobid"],
            "needRedirect": "true",
            "skipHeader": "true",
            "knowledgeid": str(job_info.get("knowledgeid", "")),
            "ktoken": job_info.get("ktoken", ""),
            "cpi": job_info.get("cpi", ""),
            "ut": "s",
            "clazzId": course["clazzId"],
            "type": "",
            "enc": job.get("enc", ""),
            "mooc2": "1",
            "courseid": course["courseId"],
        }
        for attempt in range(3):
            try:
                resp = self.session.get(_url, params=params)
            except requests.RequestException as e:
                logger.warning(f"抓题请求失败({attempt + 1}/3): {e}")
                time.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code != 200:
                time.sleep(1.0)
                continue
            if "教师未创建完成该测验" in resp.text:
                logger.info("教师未创建完成该测验，跳过")
                return None
            parsed = decode_questions_info(resp.text)
            if parsed.get("questions"):
                parsed["_work_title"] = job.get("title", "章节测验")
                return parsed
            time.sleep(1.0)
        return None

    # ---------------- 自测（self-test / exam-list） ----------------
    #
    # 真实接口（已按抓包校准），主机前缀 EXAM_HOST：
    #   题量   GET  /mooc2/exam/exam-question-count
    #   新建   POST /mooc2/exam/create-self-test        → {"taskId":..,"status":true}
    #   状态   GET  /mooc2/exam/selftest-autopapertask-status?taskId= → {"paperId":..,"taskStatus":"ok"}
    #   列表   GET  /mooc2/exam/exam-list               → HTML，含 goTest(...) 调用
    #   看卷   GET  /exam/lookPaper?...&isPreview=true   → 题目 HTML
    # 列表里每份自测卷的入口形如：
    #   goTest(courseId, tId, relationId, endTime, paperId, isRetest, enc)
    EXAM_HOST = "https://mooc1.chaoxing.com/exam-ans"

    def selftest_question_count(self, course: Dict, create_type: int = 0,
                                do_no_repeat: bool = False) -> int:
        """查询课程题库可抽题量。create_type=0 全部题库，1 错题。"""
        try:
            resp = self.session.get(
                f"{self.EXAM_HOST}/mooc2/exam/exam-question-count",
                params={
                    "courseId": course["courseId"],
                    "classId": course["clazzId"],
                    "cpi": course["cpi"],
                    "createType": create_type,
                    "doNoRepeat": str(do_no_repeat).lower(),
                },
            )
            return int(resp.json().get("count", 0))
        except Exception as e:
            logger.debug(f"查询题库题量失败: {e}")
            return 0

    def create_selftest(self, course: Dict, meta: Dict, count: int = 50,
                        title: str = "auto") -> Optional[int]:
        """
        新建一份自测卷（随机抽题，异步组卷）。返回 taskId，失败 None。
        count 受课程 maxSelfQueNum 限制（常见上限 500）。
        """
        openc = meta.get("openc", "")
        data = {
            "courseId": course["courseId"],
            "classId": course["clazzId"],
            "cpi": course["cpi"],
            "createType": 0,
            "limitTime": "",
            "openc": openc,
            "questionNum": int(count),
            "selectType": 0,
            "selectDirs": "[]",
            "selectTypes": "[]",
            "selectEasy": "[]",
            "selectTopics": "[]",
            "doNoRepeat": "false",
            "title": title,
            "selftestMode": 1,
            "recommendSet": "{}",
            "zykCourseId": 0,
            "zykEnc": "",
        }
        try:
            resp = self.session.post(
                f"{self.EXAM_HOST}/mooc2/exam/create-self-test", data=data
            )
            j = resp.json()
            if j.get("status") and j.get("taskId"):
                return int(j["taskId"])
            logger.warning(f"新建自测返回异常: {resp.text[:200]}")
        except Exception as e:
            logger.warning(f"新建自测请求异常: {e}")
        return None

    def poll_selftest_paper(self, course: Dict, task_id: int,
                            tries: int = 30, interval: float = 1.0) -> Optional[int]:
        """轮询组卷任务，taskStatus=='ok' 时返回 paperId。"""
        for _ in range(tries):
            try:
                resp = self.session.get(
                    f"{self.EXAM_HOST}/mooc2/exam/selftest-autopapertask-status",
                    params={
                        "courseId": course["courseId"],
                        "classId": course["clazzId"],
                        "cpi": course["cpi"],
                        "taskId": task_id,
                    },
                )
                j = resp.json()
                status = j.get("taskStatus")
                if status == "ok" and j.get("paperId"):
                    return int(j["paperId"])
                if status == "invalid":
                    logger.warning("组卷任务无效")
                    return None
            except Exception as e:
                logger.debug(f"轮询组卷状态异常: {e}")
            time.sleep(interval)
        logger.warning("组卷超时")
        return None

    def _parse_exam_list_entries(self, html: str) -> List[Dict]:
        """
        从 exam-list HTML 解析所有自测卷入口。
        goTest(courseId, tId, relationId, endTime, paperId, isRetest, enc)
        返回 [{tId, relationId, paperId, enc}]。
        """
        entries = []
        pattern = re.compile(
            r"goTest\(\s*'(?P<cid>[^']*)'\s*,\s*(?P<tid>\d+)\s*,\s*(?P<rid>\d+)\s*,"
            r"\s*'(?P<endtime>[^']*)'\s*,\s*(?P<pid>\d+)\s*,\s*(?P<retest>\w+)\s*,"
            r"\s*'(?P<enc>[^']*)'\s*\)"
        )
        for m in pattern.finditer(html):
            entries.append({
                "tId": m.group("tid"),
                "relationId": m.group("rid"),
                "paperId": m.group("pid"),
                "enc": m.group("enc"),
            })
        return entries

    def get_selftest_meta(self, course: Dict) -> Dict:
        """获取自测所需的课程参数（enc / examEnc / openc 等）。"""
        meta = self.get_course_meta(course)
        if not (meta.get("openc") and meta.get("examEnc")):
            # 从 exam-list 页兜底取
            try:
                resp = self.session.get(
                    f"{self.EXAM_HOST}/mooc2/exam/exam-list",
                    params={"courseid": course["courseId"], "clazzid": course["clazzId"],
                            "cpi": course["cpi"], "ut": "s"},
                )
                soup = BeautifulSoup(resp.text, "lxml")
                for k in ("openc", "examEnc", "enc"):
                    el = soup.find("input", id=k)
                    if el and el.get("value") and not meta.get(k):
                        meta[k] = el["value"]
            except Exception as e:
                logger.debug(f"取 exam meta 失败: {e}")
        return meta

    def _fetch_exam_list_html(self, course: Dict, meta: Dict) -> str:
        """按真实参数请求自测列表页 exam-list，返回 HTML。"""
        params = {
            "courseid": course["courseId"],
            "clazzid": course["clazzId"],
            "cpi": course["cpi"],
            "ut": "s",
            "t": get_timestamp(),
            "enc": meta.get("examEnc", ""),
            "openc": meta.get("openc", ""),
            "type": 1,
            "stuenc": meta.get("enc", ""),
        }
        try:
            resp = self.session.get(
                f"{self.EXAM_HOST}/mooc2/exam/exam-list", params=params
            )
            return resp.text or ""
        except requests.RequestException as e:
            logger.debug(f"请求 exam-list 失败: {e}")
            return ""

    def find_paper_entry(self, course: Dict, paper_id: int, meta: Dict) -> Optional[Dict]:
        """在 exam-list 中找到指定 paperId 对应的入口（tId/relationId/enc）。"""
        html = self._fetch_exam_list_html(course, meta)
        entries = self._parse_exam_list_entries(html)
        logger.info(f"exam-list 解析到 {len(entries)} 个自测卷入口")
        for e in entries:
            if str(e["paperId"]) == str(paper_id):
                return e
        # 没找到：保存列表 HTML 便于排查
        try:
            import os
            d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "debug_examlist.html"), "w", encoding="utf-8") as f:
                f.write(html)
            logger.warning(f"未匹配 paperId={paper_id}；已保存 exam-list HTML（{len(html)} 字符）到 output/debug_examlist.html")
        except Exception:
            pass
        return None

    def fetch_paper_html(self, course: Dict, entry: Dict, meta: Dict) -> Optional[str]:
        """获取整卷题目 HTML：先试 lookPaper 预览，再试 reVersionTestStartNew 答题页。"""
        openc = meta.get("openc", "")
        # 1) lookPaper 预览（只读）
        look_params = {
            "courseId": course["courseId"],
            "classId": course["clazzId"],
            "paperId": entry["paperId"],
            "p": 1,
            "ut": "s",
            "cpi": course["cpi"],
            "examRelationId": entry["tId"],
            "enc": entry.get("enc", ""),
            "newMooc": "true",
            "openc": openc,
            "isPreview": "true",
        }
        last_html = None
        for url, params in (
            (f"{self.EXAM_HOST}/exam/lookPaper", look_params),
            (f"{self.EXAM_HOST}/exam/test/reVersionTestStartNew", {
                "courseId": course["courseId"], "classId": course["clazzId"],
                "tId": entry["tId"], "id": entry["relationId"], "p": 1, "tag": 1,
                "enc": entry.get("enc", ""), "cpi": course["cpi"],
                "openc": openc, "newMooc": "true",
            }),
        ):
            try:
                resp = self.session.get(url, params=params)
                if resp.status_code == 200 and resp.text and (
                    "singleQuesId" in resp.text or "TiMu" in resp.text or "questionLi" in resp.text
                ):
                    return resp.text
                # 即使没命中关键字，也保留最后一次响应作为兜底
                if resp.status_code == 200:
                    last_html = resp.text
            except requests.RequestException as e:
                logger.debug(f"取卷请求失败 {url}: {e}")
        return last_html

    def fetch_selftest_once(self, course: Dict, meta: Dict, count: int = 50) -> Optional[Dict]:
        """
        完整跑一遍：新建自测 → 等组卷 → 定位入口 → 取卷 → 解析题目。
        返回 decode_questions_info 结果（含 questions），失败 None。
        同时把原始 HTML 暂存于返回值 _raw_html，便于解析失败时排查。
        """
        task_id = self.create_selftest(course, meta, count=count)
        if not task_id:
            logger.warning("create_selftest 失败")
            return None
        logger.info(f"已新建自测，taskId={task_id}")
        paper_id = self.poll_selftest_paper(course, task_id)
        if not paper_id:
            logger.warning("组卷未完成/失败")
            return None
        logger.info(f"组卷完成，paperId={paper_id}")
        entry = self.find_paper_entry(course, paper_id, meta)
        if not entry:
            return None
        logger.info(f"定位入口: tId={entry['tId']} relationId={entry['relationId']}")
        html = self.fetch_paper_html(course, entry, meta)
        if not html:
            logger.warning("取卷 HTML 为空")
            return None
        parsed = decode_questions_info(html)
        parsed["_raw_html"] = html
        parsed["_paper_id"] = paper_id
        if parsed.get("questions"):
            parsed["_work_title"] = f"{course.get('title', '')} 自测"
            return parsed
        # 解析为空也返回，便于上层保存原始 HTML 排查结构
        return parsed

