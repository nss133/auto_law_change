from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Tuple

from Levenshtein import ratio as levenshtein_ratio

from ..config.monitored_laws_loader import MonitoredLaw
from ..models import LawChangeMeta


def _normalize_name(name: str) -> str:
    """법령명 비교를 위한 정규화.

    - 괄호(부제목) 제거
    - 공백·특수기호 제거
    - '시행령', '시행규칙' 접미어 제거 → 모법과 하위법규를 한 묶음으로 취급
    - 소문자 변환
    """
    if not name:
        return ""
    # 괄호 안 부제목 제거
    name = re.sub(r"\(.*?\)", "", name)
    # 공백·특수문자 제거
    name = re.sub(r"[\s·ㆍ\-_/]", "", name)
    # 하위법규 접미어 제거 (예: 보험업법시행령 → 보험업법)
    name = re.sub(r"(시행령|시행규칙)$", "", name)
    return name.lower()


# 짧은 법령명은 한 음절 차이가 곧 다른 법령(예: 국어기본법↔국세기본법, 상법↔기상법)이라
# Levenshtein 비율만으로는 오탐이 난다. 정규화 후 길이가 이 값 미만이면 완전일치만 매칭으로 인정.
MIN_FUZZY_LEN = 10


@dataclass
class MatchResult:
    meta: LawChangeMeta
    monitored: MonitoredLaw
    score: float


def match_laws(
    monitored_laws: List[MonitoredLaw],
    metas: List[LawChangeMeta],
    threshold: float = 0.8,
) -> List[MatchResult]:
    """모니터링 대상 법령명과 수집한 법령 메타데이터를 유사도 기반으로 매칭.

    짧은 법령명(정규화 후 < ``MIN_FUZZY_LEN``)은 fuzzy 매칭 시 한 음절 차이로 다른
    법령을 오탐하므로 완전일치만 인정한다. 긴 이름만 ``threshold`` 비율 매칭을 허용한다.
    """
    results: List[MatchResult] = []

    normalized_monitored = [
        (m, _normalize_name(m.name)) for m in monitored_laws if m.name.strip()
    ]

    for meta in metas:
        norm_meta = _normalize_name(meta.law_name)
        if not norm_meta:
            continue

        best_match: Tuple[MonitoredLaw | None, str, float] = (None, "", 0.0)
        for monitored, norm_name in normalized_monitored:
            if not norm_name:
                continue
            score = levenshtein_ratio(norm_meta, norm_name)
            if score > best_match[2]:
                best_match = (monitored, norm_name, score)

        monitored, best_norm, best_score = best_match
        if monitored is None or best_score < threshold:
            continue

        # 짧은 이름은 완전일치만, 긴 이름만 fuzzy 허용
        is_exact = norm_meta == best_norm
        long_enough = min(len(norm_meta), len(best_norm)) >= MIN_FUZZY_LEN
        if is_exact or long_enough:
            results.append(MatchResult(meta=meta, monitored=monitored, score=best_score))

    return results


def augment_fsc_legislation_matches(
    monitored_laws: List[MonitoredLaw],
    in_range: List[LawChangeMeta],
    matches: List[MatchResult],
    *,
    min_norm_len: int = 12,
) -> List[MatchResult]:
    """금융위 통합 공지(한 줄에 시행령·규정 등 복수 건)처럼 전체 제목만으로는 유사도가 낮을 때 보강.

    정규화한 모니터링 법령명이 공지 제목(정규화)에 **부분 문자열**로 들어가면 매칭에 포함한다.
    """
    if not in_range:
        return matches
    matched_urls = {m.meta.detail_url for m in matches if m.meta.detail_url}
    out: List[MatchResult] = list(matches)

    for meta in in_range:
        url = meta.detail_url
        if not url or url in matched_urls:
            continue
        norm_title = _normalize_name(meta.law_name)
        for law in monitored_laws:
            nn = _normalize_name(law.name)
            if len(nn) < min_norm_len:
                continue
            if nn in norm_title:
                out.append(MatchResult(meta=meta, monitored=law, score=0.52))
                matched_urls.add(url)
                break

    return out

