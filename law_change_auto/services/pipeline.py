"""비즈니스 로직: 법령·행정규칙·입법예고 변경 수집 파이프라인."""

from __future__ import annotations

import datetime as dt
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from typing import List, Tuple

from ..config.monitored_laws_loader import MonitoredLaw
from ..fetchers.legislation_fetcher import (
    download_and_save_gosi_pdfs,
    expand_fsc_combined_notice_metas,
    fetch_fsc_legislation_list,
    fetch_notice_body_text,
)
from ..fetchers.legislation_notice_fetcher import (
    build_legislation_detail_from_moleg_for_fsc_split,
    get_legislation_notices_for_monitored,
    fetch_notice_as_detail,
)
from ..fetchers.national_law_fetcher import (
    get_admin_rule_changes_for_monitored,
    get_law_changes_for_monitored,
    get_recent_admin_rule_changes,
    get_recent_admin_rule_changes_range,
    get_recent_law_changes_range,
)
from ..fetchers.web_scraper import (
    cross_check_and_merge,
    fetch_calendar_laws,
    scrape_recent_promulgated_laws,
)
from ..fetchers.content_fetcher import (
    fetch_old_new_html,
    fetch_revision_html,
    fetch_revision_reason_from_ls_rvs_rsn_list,
)
from ..fetchers.briefing_db_fetcher import (
    get_briefing_notices_for_monitored,
    fetch_briefing_notice_detail,
)
from ..matching.law_matcher import MatchResult, augment_fsc_legislation_matches, match_laws
from ..models import LawChangeDetail, LawChangeMeta
from ..parsers.law_change_parser import parse_law_change
from ..parsers.legislation_parser import parse_reason_main_from_notice_body


def _is_valid_revision_html(html: str | None) -> bool:
    """개정이유 HTML 유효성 검사. 에러 페이지나 빈 응답이면 False."""
    if not html:
        return False
    stripped = html.strip()
    # 본문이 너무 짧으면 에러 페이지로 간주 (정상 개정이유는 최소 수백 자)
    if len(stripped) < 100:
        return False
    lower = stripped.lower()
    # 전체가 <html 태그로 시작하면 에러 페이지 (정상 응답은 개정이유 fragment)
    if lower.startswith("<!doctype") or lower.startswith("<html"):
        # 정상 개정이유 콘텐츠(rvsConScroll, contentBody)가 포함돼 있으면 유효
        if "rvsconscroll" in lower or "contentbody" in lower:
            return True
        return False
    return True


def collect_details_for_date(
    target_date: dt.date,
    monitored_laws: List[MonitoredLaw],
    law_filter: str,
    *,
    no_web_check: bool = False,
) -> List[LawChangeDetail]:
    """단일 날짜에 대한 법령·행정규칙 변경 상세를 수집한다."""
    law_metas: List[LawChangeMeta] = []
    admin_rule_metas: List[LawChangeMeta] = []

    try:
        law_metas = get_law_changes_for_monitored(monitored_laws, target_date)
        admin_from_range = get_recent_admin_rule_changes(target_date)
        admin_from_monitored = get_admin_rule_changes_for_monitored(monitored_laws, target_date)
        seen = {(m.law_name, m.admrul_seq) for m in admin_from_range}
        admin_rule_metas = list(admin_from_range)
        for m in admin_from_monitored:
            if (m.law_name, m.admrul_seq) not in seen:
                seen.add((m.law_name, m.admrul_seq))
                admin_rule_metas.append(m)
    except Exception as e:
        print(f"[law_change_auto] 경고: Open API 호출 중 오류 발생: {e}")

    all_metas: List[LawChangeMeta] = [*law_metas, *admin_rule_metas]
    print(f"[law_change_auto] 수집된 원천 변경 건수: {len(all_metas)}")

    if not no_web_check:
        # 2-1a. 달력 API (calendarInfoR) — lsi_seq 포함, 우선순위 높음
        try:
            cal_metas = fetch_calendar_laws(target_date)
            if cal_metas:
                cal_missing = cross_check_and_merge(all_metas, cal_metas)
                if cal_missing:
                    print(
                        f"[law_change_auto] 달력API 교차검증: API에 누락된 {len(cal_missing)}건 추가 발견"
                    )
                    for m in cal_missing:
                        anc = m.announcement_date.isoformat() if m.announcement_date else "-"
                        eff = m.effective_date.isoformat() if m.effective_date else "-"
                        print(f"    + {m.law_name} (공포={anc}, 시행={eff}, src={m.source})")
                    all_metas.extend(cal_missing)
                else:
                    print(
                        f"[law_change_auto] 달력API 교차검증: {len(cal_metas)}건 조회, API 누락건 없음"
                    )
            else:
                print("[law_change_auto] 달력API 교차검증: 해당 일자 공포/시행 법령 0건")
        except Exception as e:
            print(f"[law_change_auto] 경고: 달력API 교차검증 중 오류 발생 (무시): {e}")

        # 2-1b. 기존 웹 스크래핑 (lsScListR) — fallback
        try:
            web_metas = scrape_recent_promulgated_laws(target_date)
            if web_metas:
                missing = cross_check_and_merge(all_metas, web_metas)
                if missing:
                    print(
                        f"[law_change_auto] 웹 교차검증: API에 누락된 {len(missing)}건 추가 발견"
                    )
                    for m in missing:
                        anc = m.announcement_date.isoformat() if m.announcement_date else "-"
                        eff = m.effective_date.isoformat() if m.effective_date else "-"
                        print(f"    + {m.law_name} (공포={anc}, 시행={eff}, src={m.source})")
                    all_metas.extend(missing)
                else:
                    print(
                        f"[law_change_auto] 웹 교차검증: 웹 {len(web_metas)}건 조회, API 누락건 없음"
                    )
            else:
                print("[law_change_auto] 웹 교차검증: 해당 일자 공포/시행 법령 0건")
        except Exception as e:
            print(f"[law_change_auto] 경고: 웹 교차검증 중 오류 발생 (무시): {e}")

    legislation_notice_details: List[LawChangeDetail] = []
    monitored_names: List[str] = []
    try:
        monitored_names = [law.name for law in monitored_laws]
        if law_filter:
            monitored_names = [n for n in monitored_names if law_filter in n]
        notice_metas = get_legislation_notices_for_monitored(
            monitored_names, active_date=target_date
        )
        if notice_metas:
            print(f"[law_change_auto] 법제처 입법예고: {len(notice_metas)}건 발견")
            for nm in notice_metas:
                start = nm.announcement_date.isoformat() if nm.announcement_date else "-"
                end = nm.effective_date.isoformat() if nm.effective_date else "-"
                print(f"    * {nm.law_name} (예고기간={start}~{end})")
            for nm in notice_metas:
                try:
                    nd = fetch_notice_as_detail(nm)
                    if nd:
                        legislation_notice_details.append(nd)
                except Exception as e:
                    print(f"[law_change_auto] 입법예고 상세 조회 실패: {nm.law_name}: {e}")
        else:
            print("[law_change_auto] 법제처 입법예고: 해당 일자 진행 중인 건 없음")
    except Exception as e:
        print(f"[law_change_auto] 경고: 법제처 입법예고 조회 중 오류 (무시): {e}")

    # 2-3. briefing DB에서 입법예고/규정변경예고 보완 조회
    try:
        briefing_metas = get_briefing_notices_for_monitored(
            monitored_names, active_date=target_date
        )
        if briefing_metas:
            # moleg.go.kr에서 이미 찾은 건과 중복 제거 (제목 정규화 비교)
            existing_titles = set()
            for nd in legislation_notice_details:
                existing_titles.add(re.sub(r"\s+", "", nd.meta.law_name))

            new_count = 0
            for bm in briefing_metas:
                norm_title = re.sub(r"\s+", "", bm.law_name)
                # 이미 moleg에서 가져온 건이면 스킵
                if any(norm_title in et or et in norm_title for et in existing_titles):
                    continue
                try:
                    bd = fetch_briefing_notice_detail(bm)
                    if bd:
                        legislation_notice_details.append(bd)
                        existing_titles.add(norm_title)
                        new_count += 1
                except Exception as e:
                    print(f"[law_change_auto] briefing DB 상세 조회 실패: {bm.law_name}: {e}")
            if new_count:
                print(f"[law_change_auto] briefing DB 보완: {new_count}건 추가")
    except Exception as e:
        print(f"[law_change_auto] 경고: briefing DB 조회 중 오류 발생 (무시): {e}")

    if law_metas:
        print("[law_change_auto]  └ 법령 변경 목록:")
        for m in law_metas:
            anc = m.announcement_date.isoformat() if m.announcement_date else "-"
            eff = m.effective_date.isoformat() if m.effective_date else "-"
            print(f"    - {m.law_name} ({m.change_type}, 공포={anc}, 시행={eff})")

    if admin_rule_metas:
        print("[law_change_auto]  └ 행정규칙 변경 목록:")
        for m in admin_rule_metas:
            anc = m.announcement_date.isoformat() if m.announcement_date else "-"
            eff = m.effective_date.isoformat() if m.effective_date else "-"
            print(f"    - {m.law_name} ({m.change_type}, 발령={anc}, 시행={eff})")

    matches: List[MatchResult] = match_laws(monitored_laws, all_metas, threshold=0.8)

    if law_filter:
        matches = [m for m in matches if law_filter in m.meta.law_name]

    details: List[LawChangeDetail] = []
    for m in matches:
        meta = m.meta
        revision_html: str | None = None
        revision_text_from_list: str | None = None
        old_new_xml: str | None = None

        if meta.law_type == "ls" and (meta.law_id or meta.lsi_seq) and meta.effective_date:
            date_str = f"{meta.effective_date.year}. {meta.effective_date.month}. {meta.effective_date.day}."
            ann_date_str = (
                f"{meta.announcement_date.year}. {meta.announcement_date.month}. {meta.announcement_date.day}."
                if meta.announcement_date else None
            )
            chr_cls_cd = meta.chr_cls_cd or "010001"
            try:
                text, display_meta = fetch_revision_reason_from_ls_rvs_rsn_list(
                    meta.law_id, chr_cls_cd, date_str, lsi_seq=meta.lsi_seq,
                    announcement_date_str=ann_date_str,
                )
                if text:
                    revision_text_from_list = text
                if display_meta:
                    meta.law_number = display_meta.get("law_number")
                    meta.amendment_date_str = display_meta.get("amendment_date_str")
                    meta.amendment_type = display_meta.get("amendment_type")
                    lt = display_meta.get("law_type_label")
                    if lt:
                        meta.law_type_label = lt
            except Exception as e:
                print(f"[law_change_auto] 개정이유 조회 실패: {meta.law_name}: {e}")

        if not revision_text_from_list:
            try:
                revision_html = fetch_revision_html(meta)
                if not _is_valid_revision_html(revision_html):
                    if revision_html:
                        print(f"[law_change_auto] 개정이유 HTML 유효성 검사 실패 (에러 페이지): {meta.law_name}")
                    revision_html = None
            except Exception as e:
                print(f"[law_change_auto] 개정이유 조회 실패: {meta.law_name}: {e}")

        try:
            old_new_xml = fetch_old_new_html(meta)
        except Exception as e:
            print(f"[law_change_auto] 신구법비교 조회 실패: {meta.law_name}: {e}")

        if not revision_html and not revision_text_from_list and not old_new_xml:
            continue

        detail = parse_law_change(
            meta, revision_html, old_new_xml, revision_text_from_list=revision_text_from_list
        )
        details.append(detail)
        if old_new_xml and not detail.article_comparisons:
            print(f"[law_change_auto] 신구대비표 API 응답 있으나 파싱 결과 0행: {meta.law_name} lsi={meta.lsi_seq}")

    if law_filter:
        norm_filter = law_filter.replace(" ", "")
        legislation_notice_details = [
            d for d in legislation_notice_details
            if norm_filter in d.meta.law_name.replace(" ", "")
        ]
    if legislation_notice_details:
        print(f"[law_change_auto] 법제처 입법예고 안내서 포함: {len(legislation_notice_details)}건")
        details.extend(legislation_notice_details)

    # 2-4. FSC(금융위) 입법예고·규정변경예고 수집
    try:
        fsc_metas = fetch_fsc_legislation_list(max_items=50)
        # target_date ± 30일 이내 필터
        fsc_in_range = [
            m for m in fsc_metas
            if m.announcement_date
            and (target_date - dt.timedelta(days=30))
            <= m.announcement_date
            <= (target_date + dt.timedelta(days=30))
        ]
        if fsc_in_range:
            fsc_matches = match_laws(monitored_laws, fsc_in_range, threshold=0.5)
            fsc_matches = augment_fsc_legislation_matches(
                monitored_laws, fsc_in_range, fsc_matches
            )
            if law_filter:
                fsc_matches = [m for m in fsc_matches if law_filter in m.meta.law_name]

            # 기존 legislation_notice_details 법령명 정규화 집합
            existing_norm_names: set[str] = set()
            for nd in legislation_notice_details:
                existing_norm_names.add(re.sub(r"\s+", "", nd.meta.law_name))
            for nd in details:
                existing_norm_names.add(re.sub(r"\s+", "", nd.meta.law_name))

            fsc_count = 0
            body_by_url: dict[str, str] = {}
            for fm in fsc_matches:
                for meta in expand_fsc_combined_notice_metas(fm.meta):
                    norm_name = re.sub(r"\s+", "", meta.law_name)
                    if any(
                        norm_name in en or en in norm_name
                        for en in existing_norm_names
                    ):
                        continue

                    moleg_detail = build_legislation_detail_from_moleg_for_fsc_split(
                        meta
                    )
                    if moleg_detail:
                        details.append(moleg_detail)
                        existing_norm_names.add(norm_name)
                        fsc_count += 1
                        continue

                    detail_url = meta.detail_url
                    if not detail_url:
                        continue
                    if detail_url not in body_by_url:
                        try:
                            body_by_url[detail_url] = fetch_notice_body_text(detail_url)
                        except Exception as e:
                            print(
                                f"[law_change_auto] FSC 본문 조회 실패 ({meta.law_name[:30]}...): {e}"
                            )
                            body_by_url[detail_url] = ""
                    body_text = body_by_url[detail_url]
                    reason_sections, main_sections, opinion_deadline = (
                        parse_reason_main_from_notice_body(body_text)
                    )

                    combined = reason_sections + main_sections
                    details.append(
                        LawChangeDetail(
                            meta=meta,
                            reason_sections=reason_sections,
                            main_change_sections=main_sections,
                            combined_reason_and_main_sections=combined,
                            article_comparisons=[],
                            opinion_deadline=opinion_deadline,
                            comparison_pdf_paths=[],
                        )
                    )
                    existing_norm_names.add(norm_name)
                    fsc_count += 1
            if fsc_count:
                print(f"[law_change_auto] FSC 입법예고/규정변경예고: {fsc_count}건 추가")
    except Exception as e:
        print(f"[law_change_auto] 경고: FSC 입법예고 조회 중 오류 (무시): {e}")

    seen_seqs: set[str] = set()
    deduped: List[LawChangeDetail] = []
    for d in details:
        seq_key = (
            d.meta.lsi_seq
            or d.meta.admrul_seq
            or d.meta.law_id
            or f"{d.meta.law_name}_{d.meta.announcement_date}"
        )
        if seq_key in seen_seqs:
            continue
        seen_seqs.add(seq_key)
        deduped.append(d)
    if len(deduped) < len(details):
        print(f"[law_change_auto] 중복 제거: {len(details)}건 → {len(deduped)}건")
    details = deduped

    def _sort_key(d: LawChangeDetail) -> date:
        return d.meta.effective_date or d.meta.announcement_date or date.max

    details.sort(key=_sort_key)

    return details


def _fetch_detail_for_meta(
    m: MatchResult,
) -> Tuple[MatchResult, str | None, str | None, str | None]:
    """한 건 메타에 대해 개정이유·신구비교를 조회. 병렬 실행용."""
    meta = m.meta
    revision_html: str | None = None
    revision_text_from_list: str | None = None
    old_new_xml: str | None = None
    if meta.law_type == "ls" and (meta.law_id or meta.lsi_seq) and meta.effective_date:
        date_str = f"{meta.effective_date.year}. {meta.effective_date.month}. {meta.effective_date.day}."
        ann_date_str = (
            f"{meta.announcement_date.year}. {meta.announcement_date.month}. {meta.announcement_date.day}."
            if meta.announcement_date else None
        )
        chr_cls_cd = meta.chr_cls_cd or "010001"
        try:
            text, display_meta = fetch_revision_reason_from_ls_rvs_rsn_list(
                meta.law_id, chr_cls_cd, date_str, lsi_seq=meta.lsi_seq,
                announcement_date_str=ann_date_str,
            )
            if text:
                revision_text_from_list = text
            if display_meta:
                meta.law_number = display_meta.get("law_number")
                meta.amendment_date_str = display_meta.get("amendment_date_str")
                meta.amendment_type = display_meta.get("amendment_type")
                lt = display_meta.get("law_type_label")
                if lt:
                    meta.law_type_label = lt
        except Exception:
            pass
    if not revision_text_from_list:
        try:
            revision_html = fetch_revision_html(meta)
            if not _is_valid_revision_html(revision_html):
                revision_html = None
        except Exception:
            pass
    try:
        old_new_xml = fetch_old_new_html(meta)
    except Exception:
        pass
    return (m, revision_text_from_list, revision_html, old_new_xml)


def collect_details_for_range(
    date_from: dt.date,
    date_to: dt.date,
    monitored_laws: List[MonitoredLaw],
    law_filter: str,
    max_workers: int = 5,
) -> List[LawChangeDetail]:
    """기간 내 법령·행정규칙 변경 상세를 기간 API + 병렬 조회로 수집."""
    law_metas: List[LawChangeMeta] = []
    admin_rule_metas: List[LawChangeMeta] = []
    try:
        law_metas = get_recent_law_changes_range(date_from, date_to)
        admin_rule_metas = get_recent_admin_rule_changes_range(date_from, date_to)
    except Exception as e:
        print(f"[law_change_auto] 경고: Open API 호출 중 오류 발생: {e}")

    all_metas = [*law_metas, *admin_rule_metas]

    # 달력 API(calendarInfoR)로 날짜별 교차검증 — lsi_seq 포함으로 매칭 정확도 향상
    try:
        cal_all: List[LawChangeMeta] = []
        cal_seen: set[str] = set()
        current = date_from
        one_day = dt.timedelta(days=1)
        while current <= date_to:
            day_metas = fetch_calendar_laws(current)
            for m in day_metas:
                if m.lsi_seq and m.lsi_seq not in cal_seen:
                    cal_seen.add(m.lsi_seq)
                    cal_all.append(m)
            current += one_day
        if cal_all:
            cal_missing = cross_check_and_merge(all_metas, cal_all)
            if cal_missing:
                print(
                    f"[law_change_auto] 달력API 교차검증(기간): API에 누락된 {len(cal_missing)}건 추가"
                )
                all_metas.extend(cal_missing)
            else:
                print(
                    f"[law_change_auto] 달력API 교차검증(기간): {len(cal_all)}건 조회, 누락건 없음"
                )
    except Exception as e:
        print(f"[law_change_auto] 경고: 달력API 교차검증(기간) 중 오류 (무시): {e}")

    matches = match_laws(monitored_laws, all_metas, threshold=0.8)
    if law_filter:
        matches = [m for m in matches if law_filter in m.meta.law_name]

    details: List[LawChangeDetail] = []
    if matches:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_fetch_detail_for_meta, m): m for m in matches}
            for future in as_completed(futures):
                try:
                    m, revision_text, revision_html, old_new_xml = future.result()
                    if not revision_html and not revision_text and not old_new_xml:
                        continue
                    detail = parse_law_change(
                        m.meta, revision_html, old_new_xml, revision_text_from_list=revision_text
                    )
                    details.append(detail)
                except Exception:
                    pass

    # FSC(금융위) 입법예고·규정변경예고 수집
    try:
        fsc_metas = fetch_fsc_legislation_list(max_items=100)
        fsc_in_range = [
            m for m in fsc_metas
            if m.announcement_date and date_from <= m.announcement_date <= date_to
        ]
        if fsc_in_range:
            fsc_matches = match_laws(monitored_laws, fsc_in_range, threshold=0.5)
            fsc_matches = augment_fsc_legislation_matches(
                monitored_laws, fsc_in_range, fsc_matches
            )
            if law_filter:
                fsc_matches = [m for m in fsc_matches if law_filter in m.meta.law_name]

            # 기존 details 법령명 정규화 집합 (중복 제거용)
            existing_norm_names: set[str] = set()
            for d in details:
                existing_norm_names.add(re.sub(r"\s+", "", d.meta.law_name))

            fsc_count = 0
            body_by_url: dict[str, str] = {}
            for fm in fsc_matches:
                for meta in expand_fsc_combined_notice_metas(fm.meta):
                    norm_name = re.sub(r"\s+", "", meta.law_name)
                    if any(
                        norm_name in en or en in norm_name
                        for en in existing_norm_names
                    ):
                        continue

                    moleg_detail = build_legislation_detail_from_moleg_for_fsc_split(
                        meta
                    )
                    if moleg_detail:
                        details.append(moleg_detail)
                        existing_norm_names.add(norm_name)
                        fsc_count += 1
                        continue

                    detail_url = meta.detail_url
                    if not detail_url:
                        continue
                    if detail_url not in body_by_url:
                        try:
                            body_by_url[detail_url] = fetch_notice_body_text(detail_url)
                        except Exception as e:
                            print(
                                f"[law_change_auto] FSC 본문 조회 실패 ({meta.law_name[:30]}...): {e}"
                            )
                            body_by_url[detail_url] = ""
                    body_text = body_by_url[detail_url]
                    reason_sections, main_sections, opinion_deadline = (
                        parse_reason_main_from_notice_body(body_text)
                    )

                    combined = reason_sections + main_sections
                    details.append(
                        LawChangeDetail(
                            meta=meta,
                            reason_sections=reason_sections,
                            main_change_sections=main_sections,
                            combined_reason_and_main_sections=combined,
                            article_comparisons=[],
                            opinion_deadline=opinion_deadline,
                            comparison_pdf_paths=[],
                        )
                    )
                    existing_norm_names.add(norm_name)
                    fsc_count += 1
            if fsc_count:
                print(f"[law_change_auto] FSC 입법예고/규정변경예고 (기간): {fsc_count}건 추가")
    except Exception as e:
        print(f"[law_change_auto] 경고: FSC 입법예고 조회 중 오류 (무시): {e}")

    return details


def process_legislation(
    output_dir: Path,
    monitored_laws: List[MonitoredLaw],
    law_filter: str,
    max_items: int = 30,
) -> Path | None:
    """입법예고/규정변경예고 (FSC) 조회·PDF 추출·안내서 생성."""
    from ..docx_generator.generator import generate_guide

    try:
        legislation_metas = fetch_fsc_legislation_list(max_items=max_items)
    except Exception as e:
        print(f"[law_change_auto] 입법예고 목록 조회 실패: {e}")
        return None

    matches = match_laws(monitored_laws, legislation_metas, threshold=0.5)
    if law_filter:
        matches = [m for m in matches if law_filter in m.meta.law_name]
    # 매칭 없으면 제목 기반 직접 검색 (예: "자본시장" in "자본시장과 금융투자업...")
    if not matches and law_filter:
        matches = [
            MatchResult(meta=m, monitored=MonitoredLaw(name=law_filter, note=None), score=1.0)
            for m in legislation_metas
            if law_filter in m.law_name
        ]

    if not matches:
        print("[law_change_auto] 매칭되는 입법예고가 없습니다.")
        return None

    details: List[LawChangeDetail] = []
    body_by_url: dict[str, str] = {}
    for m in matches:
        for meta in expand_fsc_combined_notice_metas(m.meta):
            moleg_detail = build_legislation_detail_from_moleg_for_fsc_split(meta)
            if moleg_detail:
                details.append(moleg_detail)
                continue

            detail_url = meta.detail_url
            if not detail_url:
                continue
            if detail_url not in body_by_url:
                try:
                    body_by_url[detail_url] = fetch_notice_body_text(detail_url)
                except Exception as e:
                    print(f"[law_change_auto] 본문 조회 실패 ({meta.law_name[:30]}...): {e}")
                    body_by_url[detail_url] = ""
            body_text = body_by_url[detail_url]
            reason_sections, main_sections, opinion_deadline = parse_reason_main_from_notice_body(body_text)

            try:
                comparison_pdfs = download_and_save_gosi_pdfs(
                    detail_url, output_dir, change_type=meta.change_type
                )
            except Exception as e:
                print(f"[law_change_auto] 첨부 PDF 저장 실패 ({meta.law_name[:30]}...): {e}")
                comparison_pdfs = []

            combined = reason_sections + main_sections
            details.append(
                LawChangeDetail(
                    meta=meta,
                    reason_sections=reason_sections,
                    main_change_sections=main_sections,
                    combined_reason_and_main_sections=combined,
                    article_comparisons=[],
                    opinion_deadline=opinion_deadline,
                    comparison_pdf_paths=comparison_pdfs,
                )
            )

    if not details:
        return None

    target_date = dt.date.today()
    notice_id = (details[0].meta.law_id or "legislation").replace("#", "_")
    output_file = output_dir / f"law_change_guide_legislation_{notice_id}.docx"
    generate_guide(details, target_date, output_file)
    return output_file


def collect_legislation_details(
    output_dir: Path,
    monitored_laws: List[MonitoredLaw],
    law_filter: str,
    date_from: dt.date,
    date_to: dt.date,
    max_items: int = 100,
) -> List[LawChangeDetail]:
    """기간 내 입법예고/규정변경예고 상세 수집."""
    try:
        legislation_metas = fetch_fsc_legislation_list(max_items=max_items)
    except Exception as e:
        print(f"[law_change_auto] 입법예고 목록 조회 실패: {e}")
        return []

    # 예고일자 기준으로 기간 필터
    in_range = [
        m for m in legislation_metas
        if m.announcement_date and date_from <= m.announcement_date <= date_to
    ]
    matches = match_laws(monitored_laws, in_range, threshold=0.5)
    matches = augment_fsc_legislation_matches(monitored_laws, in_range, matches)
    if law_filter:
        matches = [m for m in matches if law_filter in m.meta.law_name]
    if not matches and law_filter:
        matches = [
            MatchResult(meta=m, monitored=MonitoredLaw(name=law_filter, note=None), score=1.0)
            for m in in_range if law_filter in m.law_name
        ]
    # 기간 모드: 매칭 없어도 기간 내 입법예고 전부 포함 (종합 안내서용)
    if not matches and in_range:
        matches = [MatchResult(meta=m, monitored=MonitoredLaw(name=m.law_name[:30], note=None), score=0.5) for m in in_range]

    details: List[LawChangeDetail] = []
    body_by_url: dict[str, str] = {}
    for m in matches:
        for meta in expand_fsc_combined_notice_metas(m.meta):
            moleg_detail = build_legislation_detail_from_moleg_for_fsc_split(meta)
            if moleg_detail:
                details.append(moleg_detail)
                continue

            detail_url = meta.detail_url
            if not detail_url:
                continue
            if detail_url not in body_by_url:
                try:
                    body_by_url[detail_url] = fetch_notice_body_text(detail_url)
                except Exception as e:
                    print(f"[law_change_auto] 본문 조회 실패 ({meta.law_name[:40]}...): {e}")
                    body_by_url[detail_url] = ""
            body_text = body_by_url[detail_url]
            reason_sections, main_sections, opinion_deadline = parse_reason_main_from_notice_body(body_text)

            try:
                comparison_pdfs = download_and_save_gosi_pdfs(
                    detail_url, output_dir, change_type=meta.change_type
                )
            except Exception as e:
                print(f"[law_change_auto] 첨부 PDF 저장 실패 ({meta.law_name[:40]}...): {e}")
                comparison_pdfs = []

            combined = reason_sections + main_sections
            details.append(
                LawChangeDetail(
                    meta=meta,
                    reason_sections=reason_sections,
                    main_change_sections=main_sections,
                    combined_reason_and_main_sections=combined,
                    article_comparisons=[],
                    opinion_deadline=opinion_deadline,
                    comparison_pdf_paths=comparison_pdfs,
                )
            )

    return details
