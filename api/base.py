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
