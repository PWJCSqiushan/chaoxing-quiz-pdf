# -*- coding: utf-8 -*-
"""Cookie 持久化（按用户隔离）。"""
import os
from typing import Dict

import requests


def cookies_path_for(uid: str) -> str:
    base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sessions")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, f"cookies_{uid}.txt")


def save_cookies(session: requests.Session, path: str) -> None:
    buffer = ""
    for k, v in session.cookies.items():
        buffer += f"{k}={v};"
    buffer = buffer.removesuffix(";")
    with open(path, "w", encoding="utf-8") as f:
        f.write(buffer)


def load_cookies(path: str) -> Dict[str, str]:
    if not path or not os.path.exists(path):
        return {}
    cookies: Dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as f:
        buffer = f.read().strip()
        if not buffer:
            return {}
        for item in buffer.split(";"):
            if "=" not in item:
                continue
            k, v = item.strip().split("=", 1)
            cookies[k] = v
    return cookies
