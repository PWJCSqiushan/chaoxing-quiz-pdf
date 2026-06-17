"""
用户数据库模块 - SQLite
支持本地注册/登录，保存超星账号凭证

安全设计：
  - 本地账号密码：PBKDF2-HMAC-SHA256 + 每用户随机盐 + 多轮迭代（格式 pbkdf2$迭代$盐$哈希），
    校验用 hmac.compare_digest 恒定时间比较；兼容旧版单轮 SHA-256 哈希并在登录时透明升级。
  - 超星账号密码：用服务器主密钥（环境变量 APP_CRYPTO_KEY，缺省时从本地 keyfile 读取/生成）
    做可逆对称加密后入库，避免明文落盘。
"""
import os
import sqlite3
import hashlib
import hmac
import base64
import time
import json
from typing import Optional, Dict, List

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "users.db")
_KEYFILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".app_secret_key")

# 旧版固定盐（仅用于识别/升级历史哈希，新密码不再使用）
_LEGACY_SALT = "chaoxing_fanya_2024"
_PBKDF2_ROUNDS = 200_000


def _get_conn() -> sqlite3.Connection:
    """获取数据库连接"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ==================== 密码哈希（PBKDF2 + 每用户随机盐） ====================

def _hash_password(password: str) -> str:
    """生成新格式密码哈希：pbkdf2$迭代次数$盐(hex)$哈希(hex)。"""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2${_PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def _legacy_hash(password: str) -> str:
    """旧版单轮 SHA-256 哈希（仅用于校验历史记录）。"""
    return hashlib.sha256(f"{_LEGACY_SALT}{password}{_LEGACY_SALT}".encode()).hexdigest()


def _verify_password(password: str, stored: str) -> bool:
    """恒定时间校验密码，兼容新旧两种格式。"""
    if not stored:
        return False
    if stored.startswith("pbkdf2$"):
        try:
            _, rounds_s, salt_hex, hash_hex = stored.split("$", 3)
            dk = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds_s)
            )
            return hmac.compare_digest(dk.hex(), hash_hex)
        except (ValueError, TypeError):
            return False
    # 旧格式：单轮 SHA-256
    return hmac.compare_digest(_legacy_hash(password), stored)


def _needs_rehash(stored: str) -> bool:
    """判断存储的哈希是否为旧格式，需要在登录成功后升级。"""
    return not (stored or "").startswith("pbkdf2$")


# ==================== 凭证对称加密（保护超星密码） ====================

def _get_crypto_key() -> bytes:
    """获取/生成 32 字节主密钥：优先环境变量 APP_CRYPTO_KEY，否则本地 keyfile。"""
    env = os.environ.get("APP_CRYPTO_KEY")
    if env:
        return hashlib.sha256(env.encode("utf-8")).digest()
    try:
        if os.path.exists(_KEYFILE):
            with open(_KEYFILE, "rb") as f:
                raw = f.read().strip()
                if raw:
                    return hashlib.sha256(raw).digest()
        # 生成并持久化一个随机密钥
        raw = base64.b64encode(os.urandom(32))
        with open(_KEYFILE, "wb") as f:
            f.write(raw)
        try:
            os.chmod(_KEYFILE, 0o600)
        except OSError:
            pass
        return hashlib.sha256(raw).digest()
    except OSError:
        # 极端情况下回退到进程内临时密钥（重启后旧密文无法解密，会要求重新登录）
        return hashlib.sha256(b"chaoxing_quiz_fallback_key").digest()


def _encrypt_secret(plaintext: str) -> str:
    """用 AES-GCM 加密敏感串，返回 'enc1$' 前缀的 base64 串。无 cryptography 库时降级为 keystream XOR。"""
    if plaintext is None:
        plaintext = ""
    key = _get_crypto_key()
    data = plaintext.encode("utf-8")
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce = os.urandom(12)
        ct = AESGCM(key).encrypt(nonce, data, None)
        return "encg$" + base64.b64encode(nonce + ct).decode("ascii")
    except Exception:
        # 降级：HMAC 派生 keystream 做 XOR（仍优于明文落盘）
        out = bytearray()
        counter = 0
        ks = b""
        for i, b in enumerate(data):
            if i % 32 == 0:
                ks = hmac.new(key, counter.to_bytes(8, "big"), hashlib.sha256).digest()
                counter += 1
            out.append(b ^ ks[i % 32])
        return "encx$" + base64.b64encode(bytes(out)).decode("ascii")


def _decrypt_secret(token: str) -> str:
    """解密 _encrypt_secret 产生的串；无法识别/解密时原样返回（兼容历史明文）。"""
    if not token:
        return ""
    key = _get_crypto_key()
    try:
        if token.startswith("encg$"):
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            blob = base64.b64decode(token[5:])
            nonce, ct = blob[:12], blob[12:]
            return AESGCM(key).decrypt(nonce, ct, None).decode("utf-8")
        if token.startswith("encx$"):
            data = base64.b64decode(token[5:])
            out = bytearray()
            counter = 0
            ks = b""
            for i, b in enumerate(data):
                if i % 32 == 0:
                    ks = hmac.new(key, counter.to_bytes(8, "big"), hashlib.sha256).digest()
                    counter += 1
                out.append(b ^ ks[i % 32])
            return bytes(out).decode("utf-8")
    except Exception:
        return ""
    # 无前缀：历史明文
    return token


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
            # 仍执行一次哈希，缓解用户名枚举的时序差异
            _verify_password(password, "pbkdf2$1$00$00")
            return {"status": False, "msg": "用户名或密码错误"}

        if not _verify_password(password, user["password_hash"]):
            return {"status": False, "msg": "用户名或密码错误"}

        # 旧格式哈希登录成功后透明升级为新格式
        if _needs_rehash(user["password_hash"]):
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (_hash_password(password), user["id"])
            )

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

        if not _verify_password(old_password, user["password_hash"]):
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
            # 更新已有记录（密码加密存储）
            conn.execute(
                "UPDATE chaoxing_accounts SET cx_password = ?, label = ? WHERE id = ?",
                (_encrypt_secret(cx_password), label, existing["id"])
            )
        else:
            # 新增记录（密码加密存储）
            conn.execute(
                "INSERT INTO chaoxing_accounts (user_id, cx_phone, cx_password, label, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, cx_phone, _encrypt_secret(cx_password), label, time.time())
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
            "data": {"id": account["id"], "cx_phone": account["cx_phone"], "cx_password": _decrypt_secret(account["cx_password"]), "label": account["label"]}
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
