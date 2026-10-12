"""Qwen-VL 图片描述：figure 的文本代理（RAG_VLM_ENABLED 时由 ingestion 调用）。

依赖 dashscope SDK（与 gte-rerank 相同依赖，requirements 未列、按需安装）。
模型默认 qwen-vl-max；可换 qwen-vl-plus（更便宜）等。
本地替代路径（A800 跑 Qwen2.5-VL 服务）留作后续扩展。
"""
from __future__ import annotations

import os

from app.config import API_KEY, RAG_VLM_MODEL

_PROMPT = (
    "You are analyzing a figure from a scientific paper. "
    "Caption: {caption}\n"
    "Describe what the figure shows: chart type, axes/labels, key components, and main takeaways. "
    "Write 3-5 concise sentences in English. Answer with the description only."
)


def _extract_text(content) -> str:
    """兼容 dashscope 返回的 str / list[{text: ...}] 两种形态。"""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return " ".join(p for p in parts if p).strip()
    return ""


def describe_image(image_path: str, *, caption: str = "") -> str:
    """对单张图片生成描述；失败返回空串（上层降级为仅 caption）。"""
    try:
        from dashscope import MultiModalConversation

        prompt = _PROMPT.format(caption=(caption or "").strip() or "(none)")
        messages = [
            {
                "role": "user",
                "content": [
                    {"image": f"file://{os.path.abspath(image_path)}"},
                    {"text": prompt},
                ],
            }
        ]
        resp = MultiModalConversation.call(
            model=RAG_VLM_MODEL,
            messages=messages,
            api_key=API_KEY,
        )
        if getattr(resp, "status_code", None) == 200:
            choices = getattr(getattr(resp, "output", None), "choices", None) or []
            if choices:
                msg = getattr(choices[0], "message", None)
                return _extract_text(getattr(msg, "content", None))
        print(
            f"[VLM] 状态异常: {getattr(resp, 'status_code', None)} "
            f"{str(getattr(resp, 'message', ''))[:160]}"
        )
    except Exception as e:
        print(f"[VLM] 调用失败: {e}")
    return ""
