from __future__ import annotations

import os
import re
from datetime import date, datetime
from typing import Dict, List

import requests
import xml.etree.ElementTree as ET

from ..models import LawChangeMeta
from ..config.monitored_laws_loader import MonitoredLaw


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


def _request_law_search(params: Dict[str, str]) -> ET.Element:
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


def get_recent_law_changes(target_date: date) -> List[LawChangeMeta]:
    """지정 일자 기준 법령(법률·대통령령·부령) 변경 목록을 조회한다.

    - 법령 체계도 목록 조회 API (target=lsStmd)
    - 시행일자(efYd)와 공포일자(ancYd)에 target_date를 범위(당일~당일)로 지정
    """
    ymd = target_date.strftime("%Y%m%d")
    metas: List[LawChangeMeta] = []
    seen_keys: set[tuple[str | None, str | None]] = set()

    # 1) 시행일자 기준
    for key, change_type in (("efYd", "시행"), ("ancYd", "공포")):
        try:
            root = _request_law_search(
                {
                    "target": "lsStmd",
                    key: f"{ymd}~{ymd}",
                    "display": "100",
                    "page": "1",
                }
            )
        except Exception:
            # 네트워크/검증 오류 시에는 조용히 넘어가고, 나머지 파이프라인은 계속 진행
            continue

        # 레코드 후보: 자식으로 '법령명' 태그를 가진 요소들
        for elem in root.iter():
            law_name = _get_child_text(elem, "법령명")
            if not law_name:
                continue

            law_id = _get_child_text(elem, "법령ID")
            chr_cls_cd = _get_child_text(elem, "개정구분코드") or _get_child_text(
                elem, "chrClsCd"
            )
            lsi_seq = _get_child_text(elem, "법령일련번호")
            anc_yd = _get_child_text(elem, "공포일자")
            ef_yd = _get_child_text(elem, "시행일자")
            detail_url = _get_child_text(elem, "본문상세링크") or _get_child_text(
                elem, "본문 상세링크"
            )
            if not lsi_seq:
                lsi_seq = _extract_lsi_seq(detail_url)

            key_tuple = (law_id, anc_yd or ef_yd)
            if key_tuple in seen_keys:
                continue
            seen_keys.add(key_tuple)

            metas.append(
                LawChangeMeta(
                    law_name=law_name,
                    category="법령",
                    change_type="시행" if change_type == "시행" else "공포",
                    announcement_date=_parse_yyyymmdd(anc_yd),
                    effective_date=_parse_yyyymmdd(ef_yd),
                    source="law.go.kr:lsStmd",
                    detail_url=detail_url,
                    law_id=law_id,
                    chr_cls_cd=chr_cls_cd,
                    law_type="ls",
                    lsi_seq=lsi_seq,
                )
            )

    return metas


def get_law_changes_for_monitored(
    monitored_laws: List[MonitoredLaw],
    target_date: date,
) -> List[LawChangeMeta]:
    """모니터링 대상 법령명을 기준으로, 해당 일자에 공포/시행된 건만 골라온다.

    - 각 법령명별로 lsStmd 검색(query=법령명, sort=efdes) 후,
    - 응답 결과에서 시행일자/공포일자가 target_date와 같은 건만 필터링.

    날짜 범위(efYd/ancYd) 기반 전체 검색이 아니라,
    '관심 대상'만 개별적으로 확인하는 방식이라 사용자 기대와 더 잘 맞는다.
    """
    metas: List[LawChangeMeta] = []
    ymd = target_date.strftime("%Y%m%d")

    for law in monitored_laws:
        name = law.name.strip()
        if not name:
            continue

        try:
            root = _request_law_search(
                {
                    "target": "lsStmd",
                    "query": name,
                    "display": "50",
                    "page": "1",
                    "sort": "efdes",  # 시행일자 기준 최신순
                }
            )
        except Exception:
            continue

        for elem in root.iter():
            law_name = _get_child_text(elem, "법령명")
            if not law_name:
                continue

            law_id = _get_child_text(elem, "법령ID")
            chr_cls_cd = _get_child_text(elem, "개정구분코드") or _get_child_text(
                elem, "chrClsCd"
            )
            lsi_seq = _get_child_text(elem, "법령일련번호")
            anc_yd = _get_child_text(elem, "공포일자")
            ef_yd = _get_child_text(elem, "시행일자")
            detail_url = _get_child_text(elem, "본문상세링크") or _get_child_text(
                elem, "본문 상세링크"
            )
            if not lsi_seq:
                lsi_seq = _extract_lsi_seq(detail_url)

            eff_date = _parse_yyyymmdd(ef_yd)
            anc_date = _parse_yyyymmdd(anc_yd)

            if eff_date == target_date:
                change_type = "시행"
            elif anc_date == target_date:
                change_type = "공포"
            else:
                continue

            metas.append(
                LawChangeMeta(
                    law_name=law_name,
                    category="법령",
                    change_type=change_type,
                    announcement_date=anc_date,
                    effective_date=eff_date,
                    source="law.go.kr:lsStmd(query)",
                    detail_url=detail_url,
                    law_id=law_id,
                    chr_cls_cd=chr_cls_cd,
                    law_type="ls",
                    lsi_seq=lsi_seq,
                )
            )

    return metas


def get_recent_admin_rule_changes(target_date: date) -> List[LawChangeMeta]:
    """지정 일자 기준 행정규칙(훈령·예규·고시 등) 변경 목록을 조회한다.

    - 행정규칙 목록 조회 API (target=admrul)
    - 발령일자 기간(prmlYd)에 target_date를 범위(당일~당일)로 지정
    """
    ymd = target_date.strftime("%Y%m%d")
    metas: List[LawChangeMeta] = []

    try:
        root = _request_law_search(
            {
                "target": "admrul",
                "prmlYd": f"{ymd}~{ymd}",
                "display": "100",
                "page": "1",
            }
        )
    except Exception:
        return metas

    for elem in root.iter():
        name = _get_child_text(elem, "행정규칙명") or _get_child_text(elem, "규칙명")
        if not name:
            continue

        rule_id = _get_child_text(elem, "행정규칙ID")
        admrul_seq = _get_child_text(elem, "행정규칙일련번호")
        prml_yd = _get_child_text(elem, "발령일자")
        ef_yd = _get_child_text(elem, "시행일자")
        detail_url = _get_child_text(elem, "본문상세링크") or _get_child_text(
            elem, "본문 상세링크"
        )
        if not admrul_seq:
            admrul_seq = _extract_admrul_seq(detail_url)

        metas.append(
            LawChangeMeta(
                law_name=name,
                category="행정규칙",
                change_type="공포",
                announcement_date=_parse_yyyymmdd(prml_yd),
                effective_date=_parse_yyyymmdd(ef_yd),
                source="law.go.kr:admrul",
                detail_url=detail_url,
                law_id=rule_id,
                law_type="admrul",
                admrul_seq=admrul_seq,
            )
        )

    return metas

