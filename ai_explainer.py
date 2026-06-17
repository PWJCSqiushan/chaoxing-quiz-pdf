# -*- coding: utf-8 -*-
"""
AI 解析生成模块（OpenAI 兼容接口）。

用户可自行配置 base_url / api_key / model，调用任意 OpenAI Chat Completions
兼容服务（OpenAI、DeepSeek、Kimi、智谱 GLM、通义千问 等），
为「只有答案、没有解析」的题目生成解析。

预设示例（用户在前端可一键选择，仅填 api_key 即可）：
  - DeepSeek : https://api.deepseek.com/v1        模型 deepseek-chat
  - OpenAI   : https://api.openai.com/v1          模型 gpt-4o-mini
  - Kimi     : https://api.moonshot.cn/v1         模型 moonshot-v1-8k
  - 智谱GLM  : https://open.bigmodel.cn/api/paas/v4 模型 glm-4-flash
  - 通义千问 : https://dashscope.aliyuncs.com/compatible-mode/v1  模型 qwen-plus
"""
import concurrent.futures
import json
import time
from typing import Callable, Dict, List, Optional

import requests

from api.logger import logger

# 前端可直接展示的预设（仅供参考，用户仍可自定义）
PRESETS = {
    "deepseek": {"label": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "openai": {"label": "OpenAI", "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "kimi": {"label": "Kimi (月之暗面)", "base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k"},
    "zhipu": {"label": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash"},
    "qwen": {"label": "通义千问", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
}

_SYSTEM_PROMPT = (
    "你是一位严谨的学科辅导老师。用户会给你一道题目、它的选项和正确答案，"
    "请你用简洁、准确的中文写出这道题的解析，说明为什么这个答案正确"
    "（必要时简述其他选项为何错误）。只输出解析正文，不要重复题目，"
    "不要使用 Markdown 标题，控制在 120 字以内。"
)


class AIExplainer:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com/v1",
        model: str = "deepseek-chat",
        temperature: float = 0.3,
        timeout: int = 40,
    ):
        self.api_key = api_key.strip()
        self.base_url = base_url.strip().rstrip("/")
        self.model = model.strip()
        self.temperature = temperature
        self.timeout = timeout

    @classmethod
    def from_config(cls, cfg: Dict) -> "AIExplainer":
        """根据配置（可含 preset）构建实例。"""
        preset_key = cfg.get("preset")
        base_url = cfg.get("base_url", "")
        model = cfg.get("model", "")
        if preset_key and preset_key in PRESETS:
            base_url = base_url or PRESETS[preset_key]["base_url"]
            model = model or PRESETS[preset_key]["model"]
        return cls(
            api_key=cfg.get("api_key", ""),
            base_url=base_url or "https://api.deepseek.com/v1",
            model=model or "deepseek-chat",
            temperature=float(cfg.get("temperature", 0.3)),
            timeout=int(cfg.get("timeout", 40)),
        )

    def _build_prompt(self, q: Dict) -> str:
        parts = [f"题目：{q.get('title', '')}"]
        opts = q.get("options") or []
        if opts:
            parts.append("选项：\n" + "\n".join(opts))
        ans = q.get("answer", "")
        parts.append(f"正确答案：{ans if ans else '（未提供，请你根据题目推断并给出答案与解析）'}")
        return "\n".join(parts)

    def explain_one(self, q: Dict, max_retries: int = 3) -> str:
        """为单题生成解析，失败返回空字符串。对 429/5xx/网络错误做指数退避重试。"""
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": self._build_prompt(q)},
            ],
            "temperature": self.temperature,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.post(url, headers=headers, data=json.dumps(payload), timeout=self.timeout)
            except requests.RequestException as e:
                logger.warning(f"AI 解析请求失败（第 {attempt}/{max_retries} 次）: {type(e).__name__}")
                if attempt < max_retries:
                    time.sleep(min(2 ** attempt, 10))
                    continue
                return ""
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
                    return content.strip()
                except (KeyError, ValueError, IndexError, TypeError, AttributeError) as e:
                    logger.warning(f"AI 解析响应解析失败: {type(e).__name__}")
                    return ""
            # 429 限流 / 5xx 服务端错误：可重试
            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = resp.headers.get("Retry-After")
                try:
                    wait = float(retry_after) if retry_after else min(2 ** attempt, 10)
                except ValueError:
                    wait = min(2 ** attempt, 10)
                logger.warning(f"AI 解析返回 {resp.status_code}（第 {attempt}/{max_retries} 次），{wait:.0f}s 后重试")
                if attempt < max_retries:
                    time.sleep(wait)
                    continue
                return ""
            # 其他 4xx（如 401/400）：不重试，仅记录状态码（避免落盘响应体）
            logger.warning(f"AI 解析返回 {resp.status_code}（不可重试）")
            return ""
        return ""

    def test_connection(self) -> Dict:
        """测试 API 连通性。"""
        q = {"title": "1+1=?", "options": ["A. 1", "B. 2", "C. 3", "D. 4"], "answer": "B"}
        text = self.explain_one(q)
        if text:
            return {"status": True, "msg": "连接成功", "sample": text}
        return {"status": False, "msg": "调用失败，请检查 API Key / URL / 模型名"}

    def explain_batch(
        self,
        questions: List[Dict],
        only_missing: bool = True,
        concurrency: int = 3,
        progress_cb: Optional[Callable[[str], None]] = None,
    ) -> int:
        """
        批量为题目生成解析，原地写入 q["analysis"]。
        only_missing=True 时仅处理没有解析的题目。
        返回成功生成的数量。
        """
        targets = [q for q in questions if (not only_missing or not q.get("analysis"))]
        if not targets:
            return 0

        done = 0
        total = len(targets)

        def work(q):
            text = self.explain_one(q)
            if text:
                q["analysis"] = text
                q["analysis_source"] = "ai"
                return True
            return False

        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
            futures = [ex.submit(work, q) for q in targets]
            for i, fut in enumerate(concurrent.futures.as_completed(futures), 1):
                try:
                    if fut.result():
                        done += 1
                except Exception as e:
                    logger.debug(f"AI 解析单题异常: {e}")
                if progress_cb and i % 3 == 0:
                    progress_cb(f"AI 解析进度 {i}/{total}（成功 {done}）")
        if progress_cb:
            progress_cb(f"AI 解析完成：成功生成 {done}/{total} 条")
        return done
