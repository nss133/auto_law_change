from __future__ import annotations

import logging
import re

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# eflaw 응답이 400KB 이상인 경우가 있어 수신 완료를 위해 30초로 설정
EFLAW_TIMEOUT = 30
from ..models import LawChangeMeta
from .law_api_common import _get_oc

HEADERS = {
    "User-Agent": "Mozilla/5.0 (law_change_auto)",
    "Referer": "https://www.law.go.kr/",
}

def _extract_reason_from_eflaw_response(resp_text: str) -> tuple[str, dict | None]:
    """eflaw API XML 응답에서 제개정이유내용과 메타데이터를 추출."""
    if not resp_text or "<html" in resp_text.lower():
        return "", None
    try:
        soup = BeautifulSoup(resp_text, "xml")
        reason_tag = None
        for tag in soup.find_all(True):
            name = getattr(tag, "name", None) or ""
            if "제개정이유내용" in name:
                reason_tag = tag
                break
        reason_text = reason_tag.get_text(strip=True) if reason_tag else ""
        reason_text = re.sub(r"\s*<법제처 제공>\s*", " ", reason_text).strip()

        metadata = None
        law_num = soup.find(lambda t: t.name and "공포번호" in (t.name or ""))
        pub_date = soup.find(lambda t: t.name and "공포일자" in (t.name or ""))
        amd_type = soup.find(lambda t: t.name and "제개정구분" in (t.name or ""))
        if law_num and pub_date and amd_type:
            pub_date_val = (pub_date.get_text(strip=True) if hasattr(pub_date, "get_text") else "") or ""
            if len(pub_date_val) == 8:
                y, m, d = pub_date_val[:4], int(pub_date_val[4:6]), int(pub_date_val[6:])
                pub_date_val = f"{y}. {m}. {d}."

            metadata = {
                "law_number": law_num.get_text(strip=True) if hasattr(law_num, "get_text") else "",
                "amendment_date_str": pub_date_val,
                "amendment_type": amd_type.get_text(strip=True) if hasattr(amd_type, "get_text") else "",
            }
        return reason_text, metadata
    except Exception as e:
        logger.debug("eflaw 응답 파싱 실패 (응답길이=%d): %s", len(resp_text) if resp_text else 0, e)
        return "", None


def _target_date_str_to_ef_yd(target_date_str: str) -> str:
    """'2026. 1. 1.' 형식 -> '20260101' (YYYYMMDD)."""
    parts = re.findall(r"\d+", target_date_str or "")
    if len(parts) >= 3:
        return f"{parts[0]}{parts[1].zfill(2)}{parts[2].zfill(2)}"
    return re.sub(r"[^0-9]", "", target_date_str or "")


def fetch_revision_reason_from_ls_rvs_rsn_list(
    ls_id: str,
    chr_cls_cd: str,
    target_date_str: str,
    lsi_seq: str | None = None,
    *,
    announcement_date_str: str | None = None,
) -> tuple[str, dict | None]:
    """
    OpenAPI target=eflaw를 사용하여 제·개정이유를 가져온다.
    ID(법령ID)로 조회 후 실패 시 MST(lsi_seq)로 재시도한다.
    efYd(시행일) 불일치 시 announcement_date(공포일) → efYd 없이 순차 재시도.
    """
    ef_yd = _target_date_str_to_ef_yd(target_date_str)
    oc = _get_oc()

    def _fetch(url: str) -> tuple[str, dict | None]:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=EFLAW_TIMEOUT)
            resp.encoding = "utf-8"
            return _extract_reason_from_eflaw_response(resp.text)
        except Exception as e:
            logger.debug("eflaw API 호출 실패: %s", e)
            return "", None

    # efYd 후보 목록: 시행일 → 공포일 → 없음(최신)
    ef_yd_candidates: list[str | None] = [ef_yd]
    if announcement_date_str:
        ann_yd = _target_date_str_to_ef_yd(announcement_date_str)
        if ann_yd and ann_yd != ef_yd:
            ef_yd_candidates.append(ann_yd)
    ef_yd_candidates.append(None)  # efYd 파라미터 없이 (최신 개정본)

    law_name_for_log = ls_id  # 로그용

    for candidate in ef_yd_candidates:
        yd_param = f"&efYd={candidate}" if candidate else ""
        url_id = (
            f"https://www.law.go.kr/DRF/lawService.do"
            f"?OC={oc}&target=eflaw&ID={ls_id}{yd_param}&chrClsCd={chr_cls_cd}&type=XML"
        )
        reason_text, metadata = _fetch(url_id)
        print(f"[eflaw] {law_name_for_log}: efYd={candidate} (ID) → {len(reason_text)}자")

        if reason_text:
            return reason_text, metadata

        if lsi_seq:
            url_mst = (
                f"https://www.law.go.kr/DRF/lawService.do"
                f"?OC={oc}&target=eflaw&MST={lsi_seq}{yd_param}&chrClsCd={chr_cls_cd}&type=XML"
            )
            reason_text, metadata = _fetch(url_mst)
            print(f"[eflaw] {law_name_for_log}: efYd={candidate} (MST) → {len(reason_text)}자")

            if reason_text:
                return reason_text, metadata

    return "", None

def fetch_revision_html(meta: LawChangeMeta) -> str | None:
    """법령/행정규칙의 제정·개정이유 HTML을 가져온다."""
    if meta.law_type == "ls" and meta.lsi_seq:
        url = f"https://www.law.go.kr/lsInfoP.do?lsiSeq={meta.lsi_seq}&viewCls=lsRvsDocInfoR"
    elif meta.law_type == "admrul" and meta.admrul_seq:
        # admRulRvsInfoR.do: 개정이유 본문만 반환 (rvsConScroll/contentBody 포함)
        url = (
            "https://www.law.go.kr/LSW/admRulRvsInfoR.do"
            f"?admRulSeq={meta.admrul_seq}"
        )
    else:
        return None

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        return resp.text
    except Exception:
        return None

def fetch_old_new_html(meta: LawChangeMeta) -> str | None:
    """법령/행정규칙의 신·구조문 대비표 XML을 가져온다."""
    oc = _get_oc()
    if meta.law_type == "ls" and meta.lsi_seq:
        url = (
            "https://www.law.go.kr/DRF/lawService.do"
            f"?OC={oc}&target=oldAndNew&MST={meta.lsi_seq}&type=XML"
        )
    elif meta.law_type == "admrul" and meta.admrul_seq:
        url = (
            "https://www.law.go.kr/DRF/lawService.do"
            f"?OC={oc}&target=admrulOldAndNew&ID={meta.admrul_seq}&type=XML"
        )
    else:
        print(
            f"[law_change_auto] 신구대비표 API 호출 스킵: {meta.law_name}"
            f" (law_type={meta.law_type}, lsi_seq={meta.lsi_seq}, admrul_seq={getattr(meta, 'admrul_seq', None)})"
        )
        return None

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        return resp.text
    except Exception:
        return None
