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
    # “自测”是超星导航里 dataname="zc" 的独立模块，data-url 指向
    #   https://mooc1.chaoxing.com/mooc2/exam/exam-list
    # 用户可“新建自测”，设置抽题数量，由系统从课程题库随机抽题。
    #
    # 下列 URL / 参数集中放在这里，便于按真实抓包结果一行校准。
    # ⚠️ 待用户提供抓包后核对：创建自测、加载题目两个接口的精确参数与字段名。

    SELFTEST_LIST_URL = "https://mooc1.chaoxing.com/mooc2/exam/exam-list"
    # 新建自测（创建一份随机抽题的自测卷）。具体字段以抓包为准。
    SELFTEST_CREATE_URL = "https://mooc1.chaoxing.com/mooc2/exam/test-create"
    # 进入自测答题页（返回题目 HTML，复用 decode_questions_info 解析）。
    SELFTEST_START_URL = "https://mooc1.chaoxing.com/mooc2/exam/exam-test-reVersionTestStartNew"

    def get_selftest_meta(self, course: Dict) -> Dict:
        """
        访问自测列表页 exam-list，解析新建自测所需的隐藏参数
        （tId / courseId / classId / cpi / examEnc 等）。

        返回 dict，至少包含课程标识；解析不到的字段留空，由抓包校准补齐。
        """
        meta = self.get_course_meta(course)
        params = {
            "courseId": course["courseId"],
            "classId": course["clazzId"],
            "cpi": course["cpi"],
            "ut": "s",
        }
        result: Dict[str, str] = dict(params)
        if meta.get("examEnc"):
            result["enc"] = meta["examEnc"]
        try:
            resp = self.session.get(self.SELFTEST_LIST_URL, params=params)
            soup = BeautifulSoup(resp.text, "lxml")
            # 常见隐藏字段
            for key in ("tId", "tid", "courseId", "classId", "clazzId", "cpi",
                        "enc", "examEnc", "personId", "userId"):
                el = soup.find("input", id=key) or soup.find("input", attrs={"name": key})
                if el and el.get("value"):
                    result[key] = el["value"]
            result["_raw_len"] = str(len(resp.text))
        except Exception as e:
            logger.debug(f"解析自测列表页失败: {e}")
        return result

    def create_selftest(self, course: Dict, meta: Dict, count: int = 20,
                         question_types: Optional[str] = None) -> Optional[Dict]:
        """
        新建一份自测卷（随机抽题）。

        count          ：抽题数量。
        question_types ：题型筛选（如需），以抓包字段为准。
        返回创建结果（含 testpaperId / testId 等用于进入答题页），失败返回 None。

        ⚠️ 字段名待抓包校准。当前按超星常见命名给出，便于快速对接。
        """
        data = {
            "courseId": course["courseId"],
            "classId": course["clazzId"],
            "cpi": course["cpi"],
            "ut": "s",
            "questionNum": count,        # 抽题数量（字段名待校准）
            "tId": meta.get("tId") or meta.get("tid", ""),
            "enc": meta.get("enc") or meta.get("examEnc", ""),
        }
        if question_types:
            data["questionType"] = question_types
        try:
            resp = self.session.post(self.SELFTEST_CREATE_URL, data=data)
            if resp.status_code != 200:
                logger.warning(f"新建自测失败: HTTP {resp.status_code}")
                return None
            # 返回可能是 JSON（含新建卷ID）或直接重定向到答题页
            try:
                j = resp.json()
                return {"raw": j, "url": resp.url}
            except ValueError:
                return {"html": resp.text, "url": resp.url}
        except requests.RequestException as e:
            logger.warning(f"新建自测请求异常: {e}")
            return None

    def fetch_selftest_questions(self, course: Dict, create_result: Dict,
                                 meta: Dict) -> Optional[Dict]:
        """
        进入自测答题页并解析题目（含答案）。

        优先使用创建自测后返回的跳转 URL；否则用 SELFTEST_START_URL 拼参数。
        复用 decode_questions_info 解析题干/选项/题型/答案。
        """
        html = None
        # 1) 创建结果直接带回了答题页 HTML
        if create_result and create_result.get("html") and "singleQuesId" in create_result["html"]:
            html = create_result["html"]
        # 2) 创建结果带回跳转 URL
        if html is None and create_result and create_result.get("url"):
            try:
                resp = self.session.get(create_result["url"])
                if resp.status_code == 200:
                    html = resp.text
            except requests.RequestException as e:
                logger.debug(f"跟随自测跳转失败: {e}")
        # 3) 兜底：用已知答题页接口
        if html is None:
            params = {
                "courseId": course["courseId"],
                "classId": course["clazzId"],
                "cpi": course["cpi"],
                "ut": "s",
                "enc": meta.get("enc") or meta.get("examEnc", ""),
            }
            raw = create_result.get("raw") if create_result else None
            if isinstance(raw, dict):
                for k in ("testpaperId", "testPaperId", "testId", "examId", "id"):
                    if raw.get("data", {}).get(k) if isinstance(raw.get("data"), dict) else raw.get(k):
                        params["testpaperId"] = (raw.get("data", {}) or raw).get(k)
                        break
            try:
                resp = self.session.get(self.SELFTEST_START_URL, params=params)
                if resp.status_code == 200:
                    html = resp.text
            except requests.RequestException as e:
                logger.debug(f"加载自测答题页失败: {e}")

        if not html:
            return None
        parsed = decode_questions_info(html)
        if parsed.get("questions"):
            parsed["_work_title"] = f"{course.get('title', '')} 自测"
            return parsed
        return None

