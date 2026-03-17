"""Perplexity API를 이용한 파급효과 문구 생성."""

from __future__ import annotations

import os
from typing import Optional

import requests

PERPLEXITY_API_URL = "https://api.perplexity.ai/chat/completions"
DEFAULT_MODEL = "sonar"
MAX_INPUT_CHARS = 1500
MAX_TOKENS = 512


def _get_api_key() -> Optional[str]:
    """환경변수 PERPLEXITY_API_KEY 반환. .env에서 load_dotenv로 로드됨."""
    return os.getenv("PERPLEXITY_API_KEY", "").strip() or None


def fetch_impact_text(
    law_name: str,
    reason_paras: list[str],
    main_paras: list[str],
    *,
    max_input_chars: int = MAX_INPUT_CHARS,
) -> Optional[str]:
    """
    법령명·개정이유·주요내용을 입력으로 Perplexity API에 파급효과 문구를 요청.
    성공 시 파급효과 텍스트 반환, 실패·빈 응답 시 None.
    """
    api_key = _get_api_key()
    if not api_key:
        return None

    reason_text = " ".join(reason_paras).strip() if reason_paras else ""
    main_text = " ".join(main_paras).strip() if main_paras else ""
    combined = f"[개정이유]\n{reason_text}\n\n[주요내용]\n{main_text}".strip()
    if len(combined) > max_input_chars:
        combined = combined[: max_input_chars - 3].rstrip() + "…"

    prompt = f"""다음 법령/규정 개정에 대한 실무 파급효과를 2~4문장으로 요약해 주세요.
법인·금융기관 등이 점검·검토해야 할 사항 위주로 간결하게 작성해 주세요.

[대상]
{law_name}

[개정 개요]
{combined or "(없음)"}
"""

    try:
        resp = requests.post(
            PERPLEXITY_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": DEFAULT_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": MAX_TOKENS,
                "temperature": 0.2,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            return None
        content = (choices[0].get("message") or {}).get("content") or ""
        text = content.strip()
        return text if len(text) >= 20 else None
    except Exception:
        return None
