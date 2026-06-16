"""
用户数据库模块 - SQLite
支持本地注册/登录，保存超星账号凭证
"""
import os
import sqlite3
import hashlib
import time
import json
from typing import Optional, Dict, List

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "users.db")


def _get_conn() -> sqlite3.Connection:
    """获取数据库连接"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _hash_password(password: str) -> str:
    """密码哈希 (SHA-256 + salt)"""
    salt = "chaoxing_fanya_2024"
    return hashlib.sha256(f"{salt}{password}{salt}".encode()).hexdigest()


def init_db():
    """初始化数据库表"""
    conn = _get_conn()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at REAL NOT NULL,
                last_login REAL
            );

            CREATE TABLE IF NOT EXISTS chaoxing_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                cx_phone TEXT NOT NULL,
                cx_password TEXT NOT NULL,
                label TEXT DEFAULT '',
                created_at REAL NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                task_id TEXT UNIQUE NOT NULL,
                status TEXT DEFAULT 'running',
                config TEXT DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL,
                offline_mode INTEGER DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
        """)
        conn.commit()
    finally:
        conn.close()


# ==================== 用户操作 ====================

def register_user(username: str, password: str) -> Dict:
    """注册新用户"""
    if not username or len(username) < 3:
        return {"status": False, "msg": "用户名至少3个字符"}
    if not password or len(password) < 6:
        return {"status": False, "msg": "密码至少6个字符"}

    conn = _get_conn()
    try:
        # 检查用户名是否已存在
        existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if existing:
            return {"status": False, "msg": "用户名已存在"}

        password_hash = _hash_password(password)
        now = time.time()
        conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, password_hash, now)
        )
        conn.commit()

        user = conn.execute("SELECT id, username, created_at FROM users WHERE username = ?", (username,)).fetchone()
        return {
            "status": True,
            "msg": "注册成功",
            "data": {"id": user["id"], "username": user["username"]}
        }
    finally:
        conn.close()


def authenticate_user(username: str, password: str) -> Dict:
    """验证用户登录"""
    if not username or not password:
        return {"status": False, "msg": "用户名或密码不能为空"}

    conn = _get_conn()
    try:
        user = conn.execute(
            "SELECT id, username, password_hash, created_at FROM users WHERE username = ?",
            (username,)
        ).fetchone()

        if not user:
            return {"status": False, "msg": "用户名或密码错误"}

        if user["password_hash"] != _hash_password(password):
            return {"status": False, "msg": "用户名或密码错误"}

        # 更新最后登录时间
        conn.execute("UPDATE users SET last_login = ? WHERE id = ?", (time.time(), user["id"]))
        conn.commit()

        return {
            "status": True,
            "msg": "登录成功",
            "data": {"id": user["id"], "username": user["username"]}
        }
    finally:
        conn.close()


def change_password(user_id: int, old_password: str, new_password: str) -> Dict:
    """修改密码"""
    if not new_password or len(new_password) < 6:
        return {"status": False, "msg": "新密码至少6个字符"}

    conn = _get_conn()
    try:
        user = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            return {"status": False, "msg": "用户不存在"}

        if user["password_hash"] != _hash_password(old_password):
            return {"status": False, "msg": "原密码错误"}

        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (_hash_password(new_password), user_id)
        )
        conn.commit()
        return {"status": True, "msg": "密码修改成功"}
    finally:
        conn.close()


# ==================== 超星账号操作 ====================

def save_chaoxing_account(user_id: int, cx_phone: str, cx_password: str, label: str = "") -> Dict:
    """保存超星账号凭证"""
    if not cx_phone or not cx_password:
        return {"status": False, "msg": "超星手机号和密码不能为空"}

    conn = _get_conn()
    try:
        # 检查是否已存在相同手机号
        existing = conn.execute(
            "SELECT id FROM chaoxing_accounts WHERE user_id = ? AND cx_phone = ?",
            (user_id, cx_phone)
        ).fetchone()

        if existing:
            # 更新已有记录
            conn.execute(
                "UPDATE chaoxing_accounts SET cx_password = ?, label = ? WHERE id = ?",
                (cx_password, label, existing["id"])
            )
        else:
            # 新增记录
            conn.execute(
                "INSERT INTO chaoxing_accounts (user_id, cx_phone, cx_password, label, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, cx_phone, cx_password, label, time.time())
            )

        conn.commit()
        return {"status": True, "msg": "超星账号保存成功"}
    finally:
        conn.close()


def get_chaoxing_accounts(user_id: int) -> Dict:
    """获取用户保存的超星账号列表"""
    conn = _get_conn()
    try:
        accounts = conn.execute(
            "SELECT id, cx_phone, label, created_at FROM chaoxing_accounts WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,)
        ).fetchall()

        return {
            "status": True,
            "data": [dict(a) for a in accounts]
        }
    finally:
        conn.close()


def get_chaoxing_account_credentials(user_id: int, account_id: int) -> Dict:
    """获取超星账号凭证（含密码，仅内部使用）"""
    conn = _get_conn()
    try:
        account = conn.execute(
            "SELECT id, cx_phone, cx_password, label FROM chaoxing_accounts WHERE user_id = ? AND id = ?",
            (user_id, account_id)
        ).fetchone()

        if not account:
            return {"status": False, "msg": "账号不存在"}

        return {
            "status": True,
            "data": {"id": account["id"], "cx_phone": account["cx_phone"], "cx_password": account["cx_password"], "label": account["label"]}
        }
    finally:
        conn.close()


def delete_chaoxing_account(user_id: int, account_id: int) -> Dict:
    """删除超星账号"""
    conn = _get_conn()
    try:
        conn.execute(
            "DELETE FROM chaoxing_accounts WHERE user_id = ? AND id = ?",
            (user_id, account_id)
        )
        conn.commit()
        return {"status": True, "msg": "删除成功"}
    finally:
        conn.close()


# ==================== 任务操作 ====================

def save_task(user_id: int, task_id: str, config: Dict = None, offline_mode: bool = False) -> Dict:
    """保存任务记录"""
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO tasks (user_id, task_id, status, config, created_at, updated_at, offline_mode) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, task_id, "running", json.dumps(config or {}, ensure_ascii=False), time.time(), time.time(), 1 if offline_mode else 0)
        )
        conn.commit()
        return {"status": True, "msg": "任务已保存"}
    finally:
        conn.close()


def update_task_status(task_id: str, status: str) -> Dict:
    """更新任务状态"""
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ?",
            (status, time.time(), task_id)
        )
        conn.commit()
        return {"status": True}
    finally:
        conn.close()


def get_user_tasks(user_id: int) -> Dict:
    """获取用户所有任务"""
    conn = _get_conn()
    try:
        tasks = conn.execute(
            "SELECT task_id, status, config, created_at, updated_at, offline_mode FROM tasks WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,)
        ).fetchall()

        return {
            "status": True,
            "data": [dict(t) for t in tasks]
        }
    finally:
        conn.close()


def get_task_by_task_id(task_id: str) -> Dict:
    """根据task_id获取任务"""
    conn = _get_conn()
    try:
        task = conn.execute(
            "SELECT task_id, user_id, status, config, created_at, updated_at, offline_mode FROM tasks WHERE task_id = ?",
            (task_id,)
        ).fetchone()

        if not task:
            return {"status": False, "msg": "任务不存在"}

        return {"status": True, "data": dict(task)}
    finally:
        conn.close()


def get_running_offline_tasks() -> List[Dict]:
    """获取所有正在运行的离线任务"""
    conn = _get_conn()
    try:
        tasks = conn.execute(
            "SELECT task_id, user_id, status, config, created_at, offline_mode FROM tasks WHERE status = 'running' AND offline_mode = 1"
        ).fetchall()
        return [dict(t) for t in tasks]
    finally:
        conn.close()


def cleanup_old_tasks(days: int = 30) -> int:
    """清理超过指定天数的旧任务"""
    conn = _get_conn()
    try:
        cutoff = time.time() - (days * 86400)
        cursor = conn.execute(
            "DELETE FROM tasks WHERE status != 'running' AND created_at < ?",
            (cutoff,)
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


# 启动时初始化数据库
init_db()
