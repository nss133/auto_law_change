"""Google Gemini API를 이용한 파급효과 문구 생성 (무료 티어 사용)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import requests

# 프로젝트 루트 .env 로드 (CLI 진입 없이 호출될 때 대비)
try:
    from dotenv import load_dotenv
    _root = Path(__file__).resolve().parents[2]  # .../law_change_auto (프로젝트 루트)
    load_dotenv(_root / ".env")
except Exception:
    pass

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_MODEL = "gemini-2.5-flash"
MAX_INPUT_CHARS = 1500
MAX_OUTPUT_TOKENS = 512


def _get_api_key() -> Optional[str]:
    """환경변수 GEMINI_API_KEY 반환."""
    return os.getenv("GEMINI_API_KEY", "").strip() or None


def fetch_impact_text(
    law_name: str,
    reason_paras: list[str],
    main_paras: list[str],
    *,
    max_input_chars: int = MAX_INPUT_CHARS,
) -> Optional[str]:
    """
    법령명·개정이유·주요내용을 입력으로 Gemini API에 파급효과 문구를 요청.
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

    prompt = f"""다음 법령/규정 개정에 대한 "파급효과" 문단을 작성해 주세요. 법령제·개정 안내서(법무팀 배포)에 들어가는 문구이므로, 기존 안내서 문체와 표현을 맞춰 주세요.

[독자·맥락]
- 생명보험회사에 있어서의 파급효과를 안내하는 문단임.
- 독자는 해당 생명보험회사의 임직원임. 즉, 본 회사 임직원을 대상으로 "우리 회사에 어떤 영향이 있는지, 무엇을 점검·반영해야 하는지"를 안내하는 톤으로 작성할 것.

[문체·표현 지침]
- 격식 있는 공문 톤으로 작성할 것.
- 파급효과 문단 전체를 "-음", "-임" 체언 종결 어미로 통일할 것. (예: "~할 것임.", "~필요가 있음.", "~반영할 것임.", "~검토함." / "~바람.", "~합니다" 등 다른 어미는 사용하지 말 것.)
- "실무 영향", "면밀히 검토", "관련 업무에 반영", "점검·검토" 등 의미는 유지하되, 문장 끝은 위와 같이 "-음", "-임"으로 끝낼 것.
- 2~4문장으로, 생명보험회사 임직원이 점검·검토해야 할 사항 위주로 구체적으로 작성할 것.
- 제목·번호(예: 파급효과, ## 등)는 쓰지 말고 본문만 작성할 것.

[대상]
{law_name}

[개정 개요]
{combined or "(없음)"}
"""

    url = f"{GEMINI_API_BASE}/{DEFAULT_MODEL}:generateContent"
    try:
        resp = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            params={"key": api_key},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "maxOutputTokens": MAX_OUTPUT_TOKENS,
                    "temperature": 0.2,
                    "thinkingConfig": {"thinkingBudget": 0},
                },
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        candidates = data.get("candidates") or []
        if not candidates:
            return None
        parts = (candidates[0].get("content") or {}).get("parts") or []
        if not parts:
            return None
        texts = []
        for part in parts:
            if part.get("thought") is True:
                continue
            t = (part.get("text") or "").strip()
            if t:
                texts.append(t)
        text = " ".join(texts).strip() if texts else ""
        for prefix in ("## 파급효과", "##파급효과", "파급효과\n", "파급효과 "):
            if text.startswith(prefix):
                text = text[len(prefix) :].strip()
                break
        return text if len(text) >= 20 else None
    except Exception:
        return None
