"""국가법령정보센터 API 공용 유틸리티.

national_law_fetcher, content_fetcher, law_body_fetcher, law_related_fetcher 등에서
공통으로 사용하는 상수·헬퍼 함수를 모아둔다.
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime

import requests
import xml.etree.ElementTree as ET


# DRF 엔드포인트 (법령/행정규칙 검색 공통)
DRF_BASE_URL = "https://www.law.go.kr/DRF/lawSearch.do"


def _get_oc() -> str:
    """OPEN API OC 값 (이메일 ID 부분)을 환경변수에서 읽는다."""
    api_key = os.getenv("LAW_GO_API_KEY")
    if not api_key:
        raise RuntimeError(
            "환경변수 'LAW_GO_API_KEY'를 찾을 수 없습니다. "
            "open.law.go.kr에서 발급받은 OC(이메일 ID 부분)를 설정해주세요."
        )
    return api_key


def _get_child_text(elem: ET.Element, tag_suffix: str) -> str | None:
    """자식 요소들 중 tag가 `*tag_suffix`로 끝나는 첫 번째 요소의 텍스트를 반환."""
    for child in elem:
        if child.tag.endswith(tag_suffix):
            text = (child.text or "").strip()
            if text:
                return text
    return None


def _parse_yyyymmdd(value: str | None) -> date | None:
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None


def _request_law_search(params: dict[str, str]) -> ET.Element:
    """lawSearch.do 호출 후 XML 파싱."""
    oc = _get_oc()
    merged = {
        "OC": oc,
        "type": "XML",
        **params,
    }
    resp = requests.get(DRF_BASE_URL, params=merged, timeout=10)
    resp.raise_for_status()
    # 국가법령정보센터는 오류 시에도 200을 돌려줄 수 있으므로, XML 내부 메시지도 한 번 확인
    root = ET.fromstring(resp.content)
    # resultCode/resultMsg가 있는 경우 실패 메시지를 확인
    code = _get_child_text(root, "resultCode")
    msg = _get_child_text(root, "resultMsg")
    if code and code != "00":
        raise RuntimeError(f"법령 검색 API 오류(resultCode={code}, resultMsg={msg})")
    return root


_LSI_SEQ_PATTERN = re.compile(r"lsiSeq=(\d+)")
_ADMRUL_SEQ_PATTERN = re.compile(r"admRulSeq=(\d+)")


def _extract_lsi_seq(detail_url: str | None) -> str | None:
    if not detail_url:
        return None
    m = _LSI_SEQ_PATTERN.search(detail_url)
    return m.group(1) if m else None


def _extract_admrul_seq(detail_url: str | None) -> str | None:
    if not detail_url:
        return None
    m = _ADMRUL_SEQ_PATTERN.search(detail_url)
    return m.group(1) if m else None
