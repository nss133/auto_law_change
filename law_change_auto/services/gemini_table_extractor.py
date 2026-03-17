"""Gemini 비전 API로 입법예고 PDF의 신구조문 대비표 이미지에서 표 텍스트를 추출한다."""

from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from typing import List, Tuple, Union

import requests

# .env 로드 (gemini_client와 동일)
try:
    from dotenv import load_dotenv
    _root = Path(__file__).resolve().parents[2]
    load_dotenv(_root / ".env")
except Exception:
    pass

from ..fetchers.pdf_extractor import render_pdf_pages_to_images
from ..models import ArticleComparisonRow

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_MODEL = "gemini-2.5-flash"
MAX_IMAGE_PAGES = 20  # 한 PDF당 최대 처리 페이지
TIMEOUT = 60


def _get_api_key() -> str | None:
    key = os.getenv("GEMINI_API_KEY", "").strip() or None
    return key


_TABLE_EXTRACT_PROMPT = """이 이미지는 법령/규정의 신구조문 대비표 한 페이지입니다.
왼쪽 열은 개정 전(구조문), 오른쪽 열은 개정 후(신조문)입니다.
표를 인식해서 각 행의 "개정 전" 텍스트와 "개정 후" 텍스트를 추출하세요.

응답은 반드시 다음 JSON 배열 형식만 출력하세요. 다른 설명이나 마크다운 코드블록 없이 순수 JSON만 출력하세요.
[{"old": "첫 번째 행의 개정 전 전문", "new": "첫 번째 행의 개정 후 전문"}, {"old": "...", "new": "..."}, ...]

빈 행은 제외하고, 실제 조문·조항이 있는 행만 포함하세요."""


def _extract_table_from_image(api_key: str, image_bytes: bytes) -> List[Tuple[str, str]]:
    """이미지 한 장을 Gemini에 보내고, 추출된 (old, new) 행 리스트를 반환한다."""
    rows: List[Tuple[str, str]] = []
    try:
        b64 = base64.standard_b64encode(image_bytes).decode("ascii")
        url = f"{GEMINI_API_BASE}/{DEFAULT_MODEL}:generateContent"
        payload = {
            "contents": [{
                "parts": [
                    {"text": _TABLE_EXTRACT_PROMPT},
                    {"inline_data": {"mime_type": "image/png", "data": b64}},
                ]
            }],
            "generationConfig": {
                "maxOutputTokens": 8192,
                "temperature": 0.1,
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        resp = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            params={"key": api_key},
            json=payload,
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        candidates = data.get("candidates") or []
        if not candidates:
            return rows
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = ""
        for part in parts:
            if part.get("thought") is True:
                continue
            t = (part.get("text") or "").strip()
            if t:
                text = t
                break
        if not text:
            return rows
        # 마크다운 코드블록 제거
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
        text = text.strip()
        arr = json.loads(text)
        if not isinstance(arr, list):
            return rows
        for item in arr:
            if not isinstance(item, dict):
                continue
            old_t = (item.get("old") or "").strip()
            new_t = (item.get("new") or "").strip()
            if not old_t and not new_t:
                continue
            if len(new_t) <= 2 and new_t not in ("개정", "신설", "삭제"):
                continue
            rows.append((old_t, new_t))
    except Exception:
        pass
    return rows


def extract_comparison_table_from_pdf(
    pdf_source: Union[Path, str, bytes],
    from_page_with_text: str = "신구조문",
    dpi: int = 150,
) -> List[ArticleComparisonRow]:
    """
    PDF를 이미지로 렌더링한 뒤 Gemini 비전으로 신구조문 대비표를 추출한다.
    pdf_source: PDF 파일 경로 또는 바이트.
    from_page_with_text: 이 텍스트가 나오는 페이지부터만 처리.
    """
    api_key = _get_api_key()
    if not api_key:
        return []

    streams = render_pdf_pages_to_images(
        pdf_source,
        dpi=dpi,
        from_page_with_text=from_page_with_text,
    )
    if not streams:
        return []

    seen: set[Tuple[str, str]] = set()
    result: List[ArticleComparisonRow] = []
    for i, stream in enumerate(streams[:MAX_IMAGE_PAGES]):
        image_bytes = stream.getvalue()
        if not image_bytes or len(image_bytes) > 18 * 1024 * 1024:  # 20MB 미만 유지
            continue
        for old_t, new_t in _extract_table_from_image(api_key, image_bytes):
            key = (old_t[:200], new_t[:200])
            if key in seen:
                continue
            seen.add(key)
            result.append(
                ArticleComparisonRow(
                    article_no=None,
                    article_title=None,
                    old_text=old_t or None,
                    new_text=new_t or None,
                    old_segments=None,
                    new_segments=None,
                )
            )
    return result


def extract_comparison_table_from_pdf_paths(
    paths: List[Tuple[str, str]],
    from_page_with_text: str = "신구조문",
) -> List[ArticleComparisonRow]:
    """
    입법예고용 (label, saved_path) 리스트에서 각 PDF를 읽어
    신구조문 대비표를 추출한 뒤 합쳐서 반환한다.
    """
    all_rows: List[ArticleComparisonRow] = []
    seen: set[Tuple[str, str]] = set()
    for label, path in paths:
        path_obj = Path(path)
        if not path_obj.exists():
            continue
        try:
            rows = extract_comparison_table_from_pdf(
                path_obj,
                from_page_with_text=from_page_with_text,
            )
            for row in rows:
                old_t = (row.old_text or "").strip()
                new_t = (row.new_text or "").strip()
                key = (old_t[:200], new_t[:200])
                if key in seen:
                    continue
                seen.add(key)
                all_rows.append(row)
        except Exception:
            continue
    return all_rows
