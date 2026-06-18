# -*- coding: utf-8 -*-
"""端到端冒烟测试：验证后端各接口骨架（不含需真实超星账号的部分）。"""
import sys
import requests

BASE = "http://127.0.0.1:5000"
s = requests.Session()
s.trust_env = False  # 不走系统代理，直连本地

def line(t): print("\n=== " + t + " ===")

ok = True

line("1. 首页静态资源")
r = s.get(BASE + "/")
print("GET / ->", r.status_code)
ok &= r.status_code == 200

line("2. 未登录访问受保护接口应 401")
r = s.get(BASE + "/api/me")
print("GET /api/me ->", r.status_code, r.json())
ok &= r.status_code == 401

line("3. 注册本地账号")
r = s.post(BASE + "/api/register", json={"username": "smoketest", "password": "test123456", "confirm_password": "test123456"})
print("register ->", r.status_code, r.json().get("msg"))
# 已存在也算通过
ok &= r.status_code in (200, 400)

line("4. 登录本地账号")
r = s.post(BASE + "/api/login", json={"username": "smoketest", "password": "test123456"})
print("login ->", r.status_code, r.json().get("msg"))
ok &= r.status_code == 200 and r.json().get("status")

line("5. 登录后 /api/me（cx 未登录）")
r = s.get(BASE + "/api/me")
j = r.json()
print("me ->", r.status_code, j.get("data"))
ok &= r.status_code == 200 and j["data"]["cx_logged_in"] is False

line("6. 未登录超星时取课程应被拦")
r = s.get(BASE + "/api/courses")
print("courses ->", r.status_code, r.json().get("msg"))
ok &= r.status_code == 403

line("7. AI 预设列表")
r = s.get(BASE + "/api/ai/presets")
j = r.json()
print("ai presets ->", r.status_code, list(j.get("data", {}).keys()))
ok &= r.status_code == 200 and "deepseek" in j.get("data", {})

line("8. 登出并验证实例释放")
r = s.post(BASE + "/api/logout")
print("logout ->", r.status_code, r.json().get("msg"))
r = s.get(BASE + "/api/me")
print("me after logout ->", r.status_code)
ok &= r.status_code == 401

print("\n" + ("ALL SMOKE TESTS PASSED" if ok else "SOME TESTS FAILED"))
sys.exit(0 if ok else 1)
