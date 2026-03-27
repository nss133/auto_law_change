"""Gemini API를 이용한 파급효과 자동 생성."""
from __future__ import annotations

import os
from typing import Optional

from ..models import LawChangeDetail


def _get_gemini_api_key() -> str | None:
    return os.getenv("GEMINI_API_KEY")


def generate_impact_analysis(detail: LawChangeDetail) -> str | None:
    """개정이유/주요내용을 기반으로 Gemini로 파급효과를 생성한다.

    Returns:
        파급효과 텍스트. API 키가 없거나 오류 시 None 반환.
    """
    api_key = _get_gemini_api_key()
    if not api_key:
        return None

    # 개정이유/주요내용 텍스트 조합
    parts: list[str] = []
    if detail.combined_reason_and_main_sections:
        parts.append("【개정이유 및 주요내용】")
        parts.extend(detail.combined_reason_and_main_sections)
    else:
        if detail.reason_sections:
            parts.append("【개정이유】")
            parts.extend(detail.reason_sections)
        if detail.main_change_sections:
            parts.append("【주요내용】")
            parts.extend(detail.main_change_sections)

    if not parts:
        return None

    law_name = detail.meta.law_name
    content = "\n".join(parts)

    # 토큰 절약: 내용이 너무 길면 앞부분만 사용
    if len(content) > 6000:
        content = content[:6000] + "\n...(이하 생략)"

    prompt = f"""당신은 한국 기업의 법무팀 법률전문가입니다.
아래는 「{law_name}」의 개정이유 및 주요내용입니다.

{content}

위 내용을 바탕으로, 이 법령 개정이 기업 실무에 미치는 **파급효과**를 간결하게 작성해 주세요.
- 기업이 준비해야 할 사항, 실무 영향, 유의점 위주로 작성
- 3~5개 항목, 각 1~2문장
- 번호 매기지 말고 자연스러운 문단으로 작성
- 존댓말 사용하지 않고 '~함', '~임' 체로 작성"""

    try:
        from google import genai

        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="models/gemini-flash-lite-latest",
            contents=prompt,
        )
        return response.text.strip() if response.text else None
    except Exception as e:
        print(f"[law_change_auto] Gemini API 오류: {e}")
        return None
