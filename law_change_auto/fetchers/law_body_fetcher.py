from __future__ import annotations

import time
from typing import Any

import requests

from .national_law_fetcher import (
    _extract_lsi_seq,
    _get_child_text,
    _get_oc,
    _request_law_search,
)

LAW_SERVICE_URL = "https://www.law.go.kr/DRF/lawService.do"

# lawService JSON 응답이 클 수 있음
LAW_SERVICE_TIMEOUT = 120


def _request_law_service_json(params: dict[str, str]) -> dict[str, Any]:
    oc = _get_oc()
    merged: dict[str, str] = {"OC": oc, "type": "JSON", **params}
    resp = requests.get(LAW_SERVICE_URL, params=merged, timeout=LAW_SERVICE_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError(f"법령 본문 API가 객체 JSON이 아닙니다: {type(data)}")
    result = data.get("result")
    msg = data.get("msg", "")
    if isinstance(result, str) and result and "실패" in result:
        raise RuntimeError(f"법령 본문 API 오류: {result} ({msg})")
    if isinstance(msg, str) and msg and ("검증에 실패" in msg or "서버장비의 IP" in msg):
        raise RuntimeError(
            f"법령 API 접근 거부: {msg} "
            "(open.law.go.kr 에서 OC 등록·허용 IP 확인)"
        )
    return data


def fetch_law_body_json(*, mst: str | None = None, law_id: str | None = None) -> dict[str, Any]:
    """현행 법령 본문을 JSON으로 조회한다 (target=law).

    MST(법령일련번호) 또는 ID(법령ID) 중 하나는 필수.
    일반적으로 최신 시행 본문은 MST가 안정적이다.
    """
    if mst:
        return _request_law_service_json({"target": "law", "MST": mst.strip()})
    if law_id:
        return _request_law_service_json({"target": "law", "ID": law_id.strip()})
    raise ValueError("mst 또는 law_id 중 하나를 지정하세요.")


def resolve_mst_and_id_from_law_name(law_name: str) -> tuple[str | None, str | None]:
    """lsStmd 검색으로 법령명에 해당하는 법령일련번호·법령ID 후보를 찾는다.

    display=1, 최신 시행순 정렬 후 첫 건을 반환 (동명이인·부분일치 주의).
    """
    name = law_name.strip()
    if not name:
        return None, None
    root = _request_law_search(
        {
            "target": "lsStmd",
            "query": name,
            "display": "1",
            "page": "1",
            "sort": "efdes",
        }
    )
    for elem in root.iter():
        found = _get_child_text(elem, "법령명")
        if not found:
            continue
        lsi_seq = _get_child_text(elem, "법령일련번호")
        detail_url = _get_child_text(elem, "본문상세링크") or _get_child_text(
            elem, "본문 상세링크"
        )
        if not lsi_seq:
            lsi_seq = _extract_lsi_seq(detail_url)
        law_id = _get_child_text(elem, "법령ID")
        return (lsi_seq, law_id)
    return None, None


def fetch_law_body_by_name(law_name: str) -> dict[str, Any]:
    """법령명으로 검색 후 첫 건 MST로 본문을 가져온다."""
    mst, law_id = resolve_mst_and_id_from_law_name(law_name)
    if mst:
        try:
            return fetch_law_body_json(mst=mst)
        except Exception:
            pass
    if law_id:
        return fetch_law_body_json(law_id=law_id)
    raise RuntimeError(f"법령명 검색으로 MST/ID를 찾지 못했습니다: {law_name!r}")
