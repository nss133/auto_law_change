"""파급효과 문구 생성: Gemini(1순위) → Groq(fallback) → None."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

import requests

# 프로젝트 루트 .env 로드 (CLI 진입 없이 호출될 때 대비)
try:
    from dotenv import load_dotenv
    _root = Path(__file__).resolve().parents[2]
    load_dotenv(_root / ".env")
except Exception:
    pass

# ── Gemini 설정 ──────────────────────────────────────────────
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_MODEL = "gemini-2.5-flash"
INTER_CALL_DELAY = 8  # 무료 티어 10 RPM 대응: 호출 간 최소 대기(초)

# ── Groq 설정 ────────────────────────────────────────────────
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"

# ── 공통 설정 ────────────────────────────────────────────────
MAX_INPUT_CHARS = 1500
MAX_OUTPUT_TOKENS = 512


def _get_gemini_key() -> Optional[str]:
    return os.getenv("GEMINI_API_KEY", "").strip() or None


def _get_groq_key() -> Optional[str]:
    return os.getenv("GROQ_API_KEY", "").strip() or None


def _build_prompt(law_name: str, combined: str) -> str:
    return f"""다음 법령/규정 개정에 대한 "파급효과" 문단을 작성해 주세요. 법령제·개정 안내서(법무팀 배포)에 들어가는 문구이므로, 기존 안내서 문체와 표현을 맞춰 주세요.

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


def _clean_text(text: str) -> Optional[str]:
    """헤더 제거 및 최소 길이 검사."""
    for prefix in ("## 파급효과", "##파급효과", "파급효과\n", "파급효과 "):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    return text if len(text) >= 12 else None


def _fetch_from_gemini(law_name: str, prompt: str) -> Optional[str]:
    """Gemini API 호출. 429 시 최대 3회 retry."""
    api_key = _get_gemini_key()
    if not api_key:
        return None

    time.sleep(INTER_CALL_DELAY)

    url = f"{GEMINI_API_BASE}/{GEMINI_MODEL}:generateContent"
    max_retries = 3
    for attempt in range(max_retries):
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
            texts = [
                (p.get("text") or "").strip()
                for p in parts
                if not p.get("thought") and (p.get("text") or "").strip()
            ]
            return _clean_text(" ".join(texts).strip()) if texts else None
        except requests.exceptions.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            if status == 429:
                wait = [5, 15, 30][attempt]
                print(f"[Gemini] {law_name}: 429 할당량 초과, {wait}초 후 재시도 ({attempt + 1}/{max_retries})")
                time.sleep(wait)
                continue
            print(f"[Gemini] {law_name}: {type(e).__name__}: {e}")
            return None
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "ResourceExhausted" in type(e).__name__:
                wait = [5, 15, 30][attempt]
                print(f"[Gemini] {law_name}: rate limit, {wait}초 후 재시도 ({attempt + 1}/{max_retries})")
                time.sleep(wait)
                continue
            print(f"[Gemini] {law_name}: {type(e).__name__}: {e}")
            return None

    print(f"[Gemini] {law_name}: {max_retries}회 재시도 모두 실패 → Groq fallback")
    return None


def _fetch_from_groq(law_name: str, prompt: str) -> Optional[str]:
    """Groq API 호출 (Gemini 실패 시 fallback)."""
    api_key = _get_groq_key()
    if not api_key:
        return None

    try:
        resp = requests.post(
            GROQ_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": GROQ_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": MAX_OUTPUT_TOKENS,
                "temperature": 0.2,
            },
            timeout=30,
        )
        resp.raise_for_status()
        text = (
            resp.json()
            .get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )
        result = _clean_text(text) if text else None
        if result:
            print(f"[Groq] {law_name}: 파급효과 생성 성공")
        return result
    except Exception as e:
        print(f"[Groq] {law_name}: {type(e).__name__}: {e}")
        return None


_COMPARISON_MAX_INPUT = 6000  # 신구조문 추출용 입력 제한 (더 긴 텍스트 허용)
_COMPARISON_MAX_OUTPUT = 2048


def _build_comparison_prompt(law_name: str, text: str) -> str:
    return f"""다음 법령 개정 텍스트에서 신·구조문 대비표를 JSON 배열로 추출하시오.

[법령명] {law_name}

[추출 규칙]
- "제N조 중 'A'를 'B'로 한다" → old_text: A, new_text: B
- "제N조를 다음과 같이 한다." 이후 조문 → old_text: null, new_text: 새 조문 전체
- "현행과 같음" 또는 "(생략)" → 해당 값 그대로 기재
- 조문번호 없는 항/호 변경도 포함
- 헤더·개정이유·부칙은 제외

[출력 형식] JSON 배열만 출력. 설명 없이.
[
  {{"article_no": "제3조", "article_title": "정의", "old_text": "기존 조문 내용", "new_text": "개정 조문 내용"}},
  {{"article_no": null, "article_title": null, "old_text": null, "new_text": "신설 조문 내용"}}
]

[텍스트]
{text}
"""


def _fetch_comparison_from_groq(law_name: str, prompt: str) -> list[dict] | None:
    """Groq으로 신구조문 JSON 추출 (빠른 응답, 비율 제한 완화)."""
    api_key = _get_groq_key()
    if not api_key:
        return None
    try:
        resp = requests.post(
            GROQ_API_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": GROQ_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": _COMPARISON_MAX_OUTPUT,
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            },
            timeout=30,
        )
        resp.raise_for_status()
        raw = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
        import json, re
        # json_object 모드는 최상위 객체를 반환할 수 있음 — 배열 추출
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
        # {"rows": [...]} 등 래핑된 경우
        for v in parsed.values():
            if isinstance(v, list):
                return v
        return None
    except Exception as e:
        print(f"[Groq] {law_name}: 신구조문 추출 실패: {e}")
        return None


def _fetch_comparison_from_gemini(law_name: str, prompt: str) -> list[dict] | None:
    """Gemini로 신구조문 JSON 추출."""
    api_key = _get_gemini_key()
    if not api_key:
        return None
    time.sleep(INTER_CALL_DELAY)
    url = f"{GEMINI_API_BASE}/{GEMINI_MODEL}:generateContent"
    try:
        resp = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            params={"key": api_key},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "maxOutputTokens": _COMPARISON_MAX_OUTPUT,
                    "temperature": 0.1,
                    "responseMimeType": "application/json",
                    "thinkingConfig": {"thinkingBudget": 0},
                },
            },
            timeout=30,
        )
        resp.raise_for_status()
        import json
        parts = (resp.json().get("candidates") or [{}])[0].get("content", {}).get("parts", [])
        raw = " ".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
        for v in parsed.values():
            if isinstance(v, list):
                return v
        return None
    except Exception as e:
        print(f"[Gemini] {law_name}: 신구조문 추출 실패: {e}")
        return None


def fetch_comparison_json(
    law_name: str,
    text: str,
    *,
    max_input_chars: int = _COMPARISON_MAX_INPUT,
) -> list[dict] | None:
    """개정문/PDF 텍스트에서 신구조문 대비표를 JSON으로 추출.

    Groq 우선(빠름) → Gemini fallback.
    반환: [{"article_no": ..., "article_title": ..., "old_text": ..., "new_text": ...}, ...]
    """
    if not text or not text.strip():
        return None
    if len(text) > max_input_chars:
        text = text[:max_input_chars].rstrip() + "…"
    prompt = _build_comparison_prompt(law_name, text)

    result = _fetch_comparison_from_groq(law_name, prompt)
    if result:
        print(f"[Groq] {law_name}: 신구조문 {len(result)}행 추출")
        return result

    result = _fetch_comparison_from_gemini(law_name, prompt)
    if result:
        print(f"[Gemini] {law_name}: 신구조문 {len(result)}행 추출")
    return result


def fetch_impact_text(
    law_name: str,
    reason_paras: list[str],
    main_paras: list[str],
    *,
    max_input_chars: int = MAX_INPUT_CHARS,
) -> Optional[str]:
    """
    파급효과 문구 생성: Gemini(1순위) → Groq(fallback) → None.
    None 반환 시 호출부에서 기본 문구로 대체.
    """
    reason_text = " ".join(reason_paras).strip() if reason_paras else ""
    main_text = " ".join(main_paras).strip() if main_paras else ""
    combined = f"[개정이유]\n{reason_text}\n\n[주요내용]\n{main_text}".strip()
    if len(combined) > max_input_chars:
        combined = combined[:max_input_chars - 3].rstrip() + "…"

    prompt = _build_prompt(law_name, combined)

    # 1순위: Gemini
    result = _fetch_from_gemini(law_name, prompt)
    if result:
        return result

    # 2순위: Groq
    return _fetch_from_groq(law_name, prompt)
