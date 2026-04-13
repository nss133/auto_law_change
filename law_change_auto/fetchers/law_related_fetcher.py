"""
국가법령정보센터 lsRlt(관련법령) API로 법률·시행령·시행규칙 묶음을 확장한다.

- 기준이 법률: 6유형(하위법) 중 이름에 「시행령」또는「시행규칙」이 있는 것만 추가,
  이후 시행령에 대해 다시 lsRlt를 돌려 시행규칙(6유형)·모법(5유형)을 연결.
- 기준이 대통령령(시행령): 5유형(상위법) 전부 + 6유형 중 「시행규칙」이 이름에 포함된 것.
- 기준이 시행규칙(총리령·부령 등): 5유형(상위법) 전부 (모법·시행령 등).
"""

from __future__ import annotations

from typing import Any

import requests

from .law_api_common import _get_oc

LAW_SEARCH_URL = "https://www.law.go.kr/DRF/lawSearch.do"


def _normalize_related(rel: Any) -> list[dict[str, Any]]:
    if rel is None:
        return []
    if isinstance(rel, list):
        return [x for x in rel if isinstance(x, dict)]
    if isinstance(rel, dict):
        return [rel]
    return []


def fetch_ls_rlt_block(*, query: str | None = None, law_id: str | None = None) -> dict[str, Any]:
    """lsRlt 검색. query 또는 ID 중 하나."""
    if not query and not law_id:
        raise ValueError("query 또는 law_id 필요")
    oc = _get_oc()
    params: dict[str, str] = {"OC": oc, "type": "JSON", "target": "lsRlt", "display": "100"}
    if law_id:
        params["ID"] = law_id.strip()
    else:
        params["query"] = (query or "").strip()
    resp = requests.get(LAW_SEARCH_URL, params=params, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("Law"):
        raise RuntimeError(str(data.get("Law")))
    block = data.get("lsRltSearch", {}).get("법령")
    if not isinstance(block, dict) or not block.get("기준법령ID"):
        raise RuntimeError(f"lsRlt 결과 없음: query={query!r} ID={law_id!r}")
    return block


def _law_stem(law_name: str) -> str:
    """「○○법률」「○○법」에서 앞부분만 뽑아, 같은 모법의 시행령·시행규칙만 고르는 데 사용."""
    n = (law_name or "").strip()
    if n.endswith(" 법률"):
        return n[: -len(" 법률")].strip()
    if n.endswith("법") and len(n) >= 2:
        return n[:-1].strip()
    return n


def _decree_stem_for_sub_rule(decree_name: str) -> str:
    """시행령 명칭에서 짝이 되는 시행규칙만 따라가기 위한 접두."""
    n = (decree_name or "").strip()
    if n.endswith(" 시행령"):
        return n[: -len(" 시행령")].strip()
    if n.endswith("시행령") and len(n) > len("시행령"):
        return n[: -len("시행령")].strip()
    return _law_stem(n)


def _kind_from_basic(basic: dict[str, Any]) -> str:
    name = str(basic.get("법령명_한글") or "")
    bc = basic.get("법종구분")
    content = bc.get("content") if isinstance(bc, dict) else str(bc or "")
    if "시행규칙" in name:
        return "rule"
    if content == "법률":
        return "law"
    if content == "대통령령" or "시행령" in name:
        return "decree"
    if content in ("총리령", "부령", "훈령"):
        return "rule"
    return "other"


def _should_follow_related(
    kind: str,
    relation_label: str,
    related_name: str,
    *,
    current_law_name: str,
) -> bool:
    rel = relation_label or ""
    rname = related_name or ""
    if kind == "law":
        if "6유형" not in rel:
            return False
        if "시행령" not in rname and "시행규칙" not in rname:
            return False
        stem = _law_stem(current_law_name)
        key = stem if len(stem) >= 2 else current_law_name
        if key and key not in rname:
            return False
        return True
    if kind == "decree":
        if "5유형" in rel:
            return True
        if "6유형" in rel and "시행규칙" in rname:
            stem = _decree_stem_for_sub_rule(current_law_name)
            key = stem if len(stem) >= 2 else current_law_name
            if key and key not in rname:
                return False
            return True
        return False
    if kind == "rule":
        return "5유형" in rel
    return False


def _normalize_ls_rlt_key(name: str) -> str:
    s = (name or "").strip()
    for ch in ("\u00a0", " "):
        s = s.replace(ch, "")
    s = s.replace("·", "").replace("ㆍ", "")
    return s


# 표기가 짧거나 띄어쓰기가 다를 때 lsRlt 가 안 맞는 경우 대비 (정규화 키 → 시도할 검색어 순서)
_LS_RLT_QUERY_ALIASES: dict[str, list[str]] = {
    "개인정보보호법": ["개인정보 보호법"],
    "남녀고용평등법": [
        "남녀고용평등과 일·가정 양립 지원에 관한 법률",
        "남녀고용평등과 일ㆍ가정 양립 지원에 관한 법률",
    ],
    "금융소비자보호에관한법률": ["금융소비자 보호에 관한 법률"],
}


def ls_rlt_query_candidates(seed_query: str) -> list[str]:
    s = (seed_query or "").strip()
    if not s:
        return []
    key = _normalize_ls_rlt_key(s)
    extra = _LS_RLT_QUERY_ALIASES.get(key, [])
    out: list[str] = []
    for q in [s, *extra]:
        if q and q not in out:
            out.append(q)
    return out


def expand_law_family_law_ids(seed_query: str) -> list[tuple[str, str]]:
    """
    모니터링 표기명(법령명)으로 시작해, 법률+시행령+시행규칙에 해당하는 법령ID 목록을 반환.
    (id, 법령명_한글) — 본문 조회는 fetch_law_body_json(law_id=).
    """
    seed_query = seed_query.strip()
    if not seed_query:
        return []

    block: dict[str, Any] | None = None
    last_err: Exception | None = None
    for q in ls_rlt_query_candidates(seed_query):
        try:
            block = fetch_ls_rlt_block(query=q)
            break
        except Exception as e:
            last_err = e
    if block is None:
        raise RuntimeError(f"lsRlt 실패(후보 검색 모두 실패): {seed_query!r} — {last_err}")
    queue: list[str] = [str(block["기준법령ID"]).strip()]
    ordered: list[tuple[str, str]] = []
    seen: set[str] = set()

    # 지연 import 순환 방지
    from law_change_auto.fetchers.law_body_fetcher import fetch_law_body_json

    def _unwrap_root(payload: dict) -> dict:
        return payload.get("법령", payload)

    while queue:
        lid = queue.pop(0)
        if lid in seen:
            continue
        try:
            payload = fetch_law_body_json(law_id=lid)
        except Exception:
            continue
        seen.add(lid)
        root = _unwrap_root(payload)
        basic = root.get("기본정보")
        if not isinstance(basic, dict):
            continue
        name = str(basic.get("법령명_한글") or "").strip()
        ordered.append((lid, name))
        kind = _kind_from_basic(basic)

        try:
            rb = fetch_ls_rlt_block(law_id=lid)
        except Exception:
            continue
        for item in _normalize_related(rb.get("관련법령")):
            rid = str(item.get("관련법령ID") or "").strip()
            rname = str(item.get("관련법령명") or "")
            rtype = str(item.get("법령간관계") or "")
            if not rid or rid in seen:
                continue
            if _should_follow_related(kind, rtype, rname, current_law_name=name):
                queue.append(rid)

    return ordered
