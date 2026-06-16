# -*- coding: utf-8 -*-
"""
超星自测题库 → PDF  Web 后端。

设计要点：
  - 本地用户体系（注册/登录）复用 api/database.py。
  - 每个登录会话持有独立的 Chaoxing 实例（按 user_id 隔离 cookie），支持多用户。
  - 抓题任务在后台线程运行，前端通过轮询 /api/task/<id> 获取进度与结果��
  - PDF 生成后提供在线预览（inline）与下载（attachment），兼容手机微信/QQ 内置浏览器。
"""
import os
import sys
import threading
import time
import uuid
from typing import Dict, Optional

from flask import (
    Flask, request, jsonify, session, send_file, Response, abort
)
from flask_cors import CORS

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from api.base import Chaoxing, Account
from api.logger import logger
from api.database import (
    register_user, authenticate_user,
    save_chaoxing_account, get_chaoxing_account_credentials,
)
from quiz_fetcher import QuizFetcher
from pdf_builder import build_quiz_pdf

STATIC_DIR = os.path.join(SCRIPT_DIR, "web", "dist")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

if os.path.exists(STATIC_DIR):
    app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="")
else:
    # 开发期回退到 web/ 源目录（纯静态前端）
    fallback = os.path.join(SCRIPT_DIR, "web")
    app = Flask(__name__, static_folder=fallback, static_url_path="")

app.secret_key = os.environ.get("FLASK_SECRET_KEY", "chaoxing_quiz_pdf_secret_2026")
app.permanent_session_lifetime = 60 * 60 * 24 * 7
CORS(app, supports_credentials=True)

# 内存态：user_id -> Chaoxing 实例
_cx_instances: Dict[int, Chaoxing] = {}
_cx_lock = threading.Lock()

# 内存态：task_id -> 任务信息
_tasks: Dict[str, dict] = {}
_tasks_lock = threading.Lock()


# ==================== 工具 ====================

def _current_user() -> Optional[int]:
    return session.get("user_id")


def _require_login():
    uid = _current_user()
    if not uid:
        abort(401)
    return uid


def _get_cx(uid: int) -> Optional[Chaoxing]:
    with _cx_lock:
        return _cx_instances.get(uid)


def _ok(data=None, msg="ok"):
    return jsonify({"status": True, "msg": msg, "data": data})


def _err(msg, code=400):
    return jsonify({"status": False, "msg": msg}), code


# ==================== 本地用户认证 ====================

@app.route("/api/register", methods=["POST"])
def api_register():
    data = request.json or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    confirm = data.get("confirm_password", password)
    if password != confirm:
        return _err("两次输入的密码不一致")
    result = register_user(username, password)
    return (jsonify(result), 200) if result["status"] else (jsonify(result), 400)


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.json or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    result = authenticate_user(username, password)
    if not result["status"]:
        return jsonify(result), 400
    session.permanent = True
    session["user_id"] = result["data"]["id"]
    session["username"] = result["data"]["username"]
    return jsonify(result)


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return _ok(msg="已退出登录")


@app.route("/api/me", methods=["GET"])
def api_me():
    uid = _current_user()
    if not uid:
        return _err("未登录", 401)
    cx = _get_cx(uid)
    return _ok({
        "user_id": uid,
        "username": session.get("username"),
        "cx_logged_in": bool(cx and cx.get_uid()),
    })


# ==================== 超星账号登录 ====================

@app.route("/api/cx/login", methods=["POST"])
def api_cx_login():
    uid = _require_login()
    data = request.json or {}
    phone = (data.get("phone") or "").strip()
    cx_password = data.get("password") or ""
    remember = bool(data.get("remember", True))

    # 支持用已保存账号免输密码
    if not phone and data.get("account_id"):
        cred = get_chaoxing_account_credentials(uid, int(data["account_id"]))
        if cred["status"]:
            phone = cred["data"]["cx_phone"]
            cx_password = cred["data"]["cx_password"]

    if not phone or not cx_password:
        return _err("请输入超星手机号与密码")

    account = Account(phone, cx_password)
    cx = Chaoxing(account=account, session_key=str(uid))
    result = cx.login(login_with_cookies=False)
    if not result["status"]:
        return _err(result["msg"])

    with _cx_lock:
        _cx_instances[uid] = cx
    if remember:
        save_chaoxing_account(uid, phone, cx_password)
    return _ok(msg="超星登录成功")


# ==================== 课程 / 章节 ====================

@app.route("/api/courses", methods=["GET"])
def api_courses():
    uid = _require_login()
    cx = _get_cx(uid)
    if not cx:
        return _err("请先登录超星账号", 403)
    try:
        courses = cx.get_course_list()
    except Exception as e:
        logger.error(f"获取课程失败: {e}")
        return _err(f"获取课程失败: {e}", 500)
    slim = [{
        "courseId": c["courseId"], "clazzId": c["clazzId"], "cpi": c["cpi"],
        "title": c["title"], "teacher": c.get("teacher", ""),
    } for c in courses]
    return _ok(slim)


@app.route("/api/chapters", methods=["POST"])
def api_chapters():
    uid = _require_login()
    cx = _get_cx(uid)
    if not cx:
        return _err("请先登录超星账号", 403)
    data = request.json or {}
    try:
        point_data = cx.get_course_point(data["courseId"], data["clazzId"], data["cpi"])
    except Exception as e:
        return _err(f"获取章节失败: {e}", 500)
    points = [{"id": p["id"], "title": p["title"]} for p in point_data.get("points", [])]
    return _ok(points)


# ==================== 抓题任务 ====================

def _run_fetch_task(task_id: str, uid: int, payload: dict):
    cx = _get_cx(uid)
    task = _tasks[task_id]

    def progress(msg: str):
        with _tasks_lock:
            task["logs"].append({"t": time.time(), "msg": msg})
            task["message"] = msg

    try:
        fetcher = QuizFetcher(cx, progress_cb=progress)
        course = payload["course"]
        mode = payload.get("mode", "accumulate")

        if mode == "single":
            point = payload["point"]
            questions = fetcher.fetch_single_chapter(course, point)
        else:
            questions = fetcher.fetch_course_accumulate(
                course,
                rounds=int(payload.get("rounds", 3)),
                concurrency=int(payload.get("concurrency", 3)),
                target=int(payload.get("target", 0)),
            )

        if not questions:
            with _tasks_lock:
                task["status"] = "failed"
                task["message"] = "未抓取到任何题目（可能该课程无开放测验，或题库为空）"
            return

        progress(f"共抓取 {len(questions)} 题，正在生成 PDF…")
        title = payload.get("title") or f"{course.get('title', '超星')} 自测试卷"
        out_name = f"{task_id}.pdf"
        out_path = os.path.join(OUTPUT_DIR, out_name)
        build_quiz_pdf(
            questions, out_path,
            title=title,
            course_name=course.get("title", ""),
            include_answers=bool(payload.get("include_answers", True)),
        )
        with _tasks_lock:
            task["status"] = "done"
            task["message"] = f"完成，共 {len(questions)} 题"
            task["count"] = len(questions)
            task["pdf"] = out_name
    except Exception as e:
        logger.exception("抓题任务失败")
        with _tasks_lock:
            task["status"] = "failed"
            task["message"] = f"任务失败: {e}"


@app.route("/api/fetch", methods=["POST"])
def api_fetch():
    uid = _require_login()
    cx = _get_cx(uid)
    if not cx:
        return _err("请先登录超星账号", 403)
    payload = request.json or {}
    if not payload.get("course"):
        return _err("缺少课程信息")

    task_id = uuid.uuid4().hex[:16]
    with _tasks_lock:
        _tasks[task_id] = {
            "id": task_id, "uid": uid, "status": "running",
            "message": "任务已创建", "logs": [], "count": 0, "pdf": None,
            "created": time.time(),
        }
    threading.Thread(target=_run_fetch_task, args=(task_id, uid, payload), daemon=True).start()
    return _ok({"task_id": task_id})


@app.route("/api/task/<task_id>", methods=["GET"])
def api_task(task_id):
    uid = _require_login()
    with _tasks_lock:
        task = _tasks.get(task_id)
        if not task or task["uid"] != uid:
            return _err("任务不存在", 404)
        # 只回传最近若干条日志
        return _ok({
            "id": task["id"],
            "status": task["status"],
            "message": task["message"],
            "count": task["count"],
            "pdf": task["pdf"],
            "logs": [l["msg"] for l in task["logs"][-40:]],
        })


@app.route("/api/task/<task_id>/stream")
def api_task_stream(task_id):
    """SSE 进度流（可选，前端优先用轮询，移动端兼容性更好）。"""
    uid = _require_login()

    def gen():
        last = 0
        while True:
            with _tasks_lock:
                task = _tasks.get(task_id)
                if not task or task["uid"] != uid:
                    yield "event: error\ndata: 任务不存在\n\n"
                    return
                logs = task["logs"]
                while last < len(logs):
                    yield f"data: {logs[last]['msg']}\n\n"
                    last += 1
                status = task["status"]
            if status in ("done", "failed"):
                yield f"event: {status}\ndata: {status}\n\n"
                return
            time.sleep(1)

    return Response(gen(), mimetype="text/event-stream")


# ==================== PDF 下载 / 预览 ====================

def _safe_pdf_path(task_id: str, uid: int) -> Optional[str]:
    with _tasks_lock:
        task = _tasks.get(task_id)
        if not task or task["uid"] != uid or not task.get("pdf"):
            return None
        pdf_name = task["pdf"]
    path = os.path.join(OUTPUT_DIR, pdf_name)
    return path if os.path.exists(path) else None


@app.route("/api/task/<task_id>/pdf", methods=["GET"])
def api_task_pdf(task_id):
    """inline 预览（微信/QQ 内置浏览器可直接打开，再用"其他应用打开/转发"）。"""
    uid = _require_login()
    path = _safe_pdf_path(task_id, uid)
    if not path:
        return _err("PDF 未就绪", 404)
    download = request.args.get("download") == "1"
    filename = request.args.get("name") or "超星自测试卷.pdf"
    if not filename.endswith(".pdf"):
        filename += ".pdf"
    return send_file(
        path, mimetype="application/pdf",
        as_attachment=download, download_name=filename,
    )


# ==================== 前端静态资源 ====================

@app.route("/")
def index():
    return app.send_static_file("index.html")


@app.errorhandler(404)
def spa_fallback(e):
    # 非 API 路径回退到前端首页（SPA）
    if request.path.startswith("/api/"):
        return jsonify({"status": False, "msg": "接口不存在"}), 404
    try:
        return app.send_static_file("index.html")
    except Exception:
        return jsonify({"status": False, "msg": "前端未构建"}), 404


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"启动服务: http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, threaded=True)
