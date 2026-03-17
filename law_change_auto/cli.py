from __future__ import annotations

import argparse
import datetime as dt
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Set, Tuple

from dotenv import load_dotenv

# 프로젝트 루트의 .env에서 LAW_GO_API_KEY, GEMINI_API_KEY 등 로드
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from .config.monitored_laws_loader import MonitoredLaw, load_monitored_laws
from .docx_generator.generator import generate_guide
from .fetchers.legislation_fetcher import (
    download_and_save_gosi_pdfs,
    fetch_fsc_legislation_list,
    fetch_notice_body_text,
)
from .fetchers.national_law_fetcher import (
    get_admin_rule_changes_for_monitored,
    get_law_changes_for_monitored,
    get_recent_admin_rule_changes,
    get_recent_admin_rule_changes_range,
    get_recent_law_changes_range,
)
from .fetchers.content_fetcher import (
    fetch_old_new_html,
    fetch_revision_html,
    fetch_revision_reason_from_ls_rvs_rsn_list,
)
from .matching.law_matcher import MatchResult, match_laws
from .models import LawChangeDetail, LawChangeMeta
from .parsers.law_change_parser import parse_law_change
from .parsers.legislation_parser import parse_reason_main_from_notice_body
from .services.gemini_table_extractor import extract_comparison_table_from_pdf_paths


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="국가법령정보센터 기준 법령/행정규칙/입법예고 변경을 조회하여 법령제·개정 안내서(docx)를 생성합니다."
    )
    parser.add_argument(
        "--date",
        type=str,
        default="today",
        help="기준 일자 (예: 2026-02-26, 기본값: today). --date-from/--date-to 사용 시 무시됨.",
    )
    parser.add_argument(
        "--date-from",
        type=str,
        default="",
        help="기간 시작일 (예: 2025-10-01). --date-to와 함께 사용 시 기간 내 각 날짜별 DOCX 생성.",
    )
    parser.add_argument(
        "--date-to",
        type=str,
        default="",
        help="기간 종료일 (예: 2025-10-31). --date-from과 함께 사용.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="실제 DOCX 생성 없이 콘솔 로그만 출력합니다.",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="output",
        help="안내서 DOCX 파일을 저장할 디렉터리 (기본: ./output)",
    )
    parser.add_argument(
        "--law",
        type=str,
        default="",
        help="특정 법령명에 대해 안내서를 생성할 때 사용 (부분일치, 예: 보험업법)",
    )
    parser.add_argument(
        "--legislation",
        action="store_true",
        help="입법예고/규정변경예고 모드. 금융위원회 FSC 목록에서 매칭 건의 PDF 첨부를 추출해 안내서 생성.",
    )
    parser.add_argument(
        "--no-perplexity",
        action="store_true",
        help="파급효과를 Perplexity API로 생성하지 않고 기본 문구만 사용 (비용 절감용).",
    )
    return parser.parse_args(argv)


def _resolve_target_date(value: str) -> dt.date:
    if value == "today":
        return dt.date.today()
    return dt.date.fromisoformat(value)


def _date_range(date_from: dt.date, date_to: dt.date):
    """date_from ~ date_to (포함) 범위의 날짜를 하루씩 yield."""
    current = date_from
    while current <= date_to:
        yield current
        current += dt.timedelta(days=1)


def _collect_details_for_date(
    target_date: dt.date,
    monitored_laws: List[MonitoredLaw],
    law_filter: str,
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
    matches: List[MatchResult] = match_laws(monitored_laws, all_metas, threshold=0.8)

    if law_filter:
        matches = [m for m in matches if law_filter in m.meta.law_name]

    details: List[LawChangeDetail] = []
    for m in matches:
        meta = m.meta
        revision_html: str | None = None
        revision_text_from_list: str | None = None
        old_new_xml: str | None = None

        if meta.law_type == "ls" and meta.law_id and meta.effective_date:
            date_str = f"{meta.effective_date.year}. {meta.effective_date.month}. {meta.effective_date.day}."
            chr_cls_cd = meta.chr_cls_cd or "010001"
            try:
                text, display_meta = fetch_revision_reason_from_ls_rvs_rsn_list(
                    meta.law_id, chr_cls_cd, date_str, lsi_seq=meta.lsi_seq
                )
                if text:
                    revision_text_from_list = text
                if display_meta:
                    meta.law_number = display_meta.get("law_number")
                    meta.amendment_date_str = display_meta.get("amendment_date_str")
                    meta.amendment_type = display_meta.get("amendment_type")
            except Exception as e:
                print(f"[law_change_auto] 개정이유 조회 실패: {meta.law_name}: {e}")

        if not revision_text_from_list:
            try:
                revision_html = fetch_revision_html(meta)
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

    return details


def _fetch_detail_for_meta(
    m: MatchResult,
) -> Tuple[MatchResult, str | None, str | None, str | None]:
    """한 건 메타에 대해 개정이유·신구비교를 조회. 병렬 실행용."""
    meta = m.meta
    revision_html: str | None = None
    revision_text_from_list: str | None = None
    old_new_xml: str | None = None
    if meta.law_type == "ls" and meta.law_id and meta.effective_date:
        date_str = f"{meta.effective_date.year}. {meta.effective_date.month}. {meta.effective_date.day}."
        chr_cls_cd = meta.chr_cls_cd or "010001"
        try:
            text, display_meta = fetch_revision_reason_from_ls_rvs_rsn_list(
                meta.law_id, chr_cls_cd, date_str, lsi_seq=meta.lsi_seq
            )
            if text:
                revision_text_from_list = text
            if display_meta:
                meta.law_number = display_meta.get("law_number")
                meta.amendment_date_str = display_meta.get("amendment_date_str")
                meta.amendment_type = display_meta.get("amendment_type")
        except Exception:
            pass
    if not revision_text_from_list:
        try:
            revision_html = fetch_revision_html(meta)
        except Exception:
            pass
    try:
        old_new_xml = fetch_old_new_html(meta)
    except Exception:
        pass
    return (m, revision_text_from_list, revision_html, old_new_xml)


def _collect_details_for_range(
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
    matches = match_laws(monitored_laws, all_metas, threshold=0.8)
    if law_filter:
        matches = [m for m in matches if law_filter in m.meta.law_name]

    details: List[LawChangeDetail] = []
    if not matches:
        return details

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
    return details


def _process_single_date(
    target_date: dt.date,
    output_dir: Path,
    monitored_laws: List[MonitoredLaw],
    law_filter: str,
    create_example_if_empty: bool = True,
    use_perplexity: bool = True,
) -> Path | None:
    """단일 날짜에 대한 변경 조회·파싱·DOCX 생성. 생성된 파일 경로를 반환하고, 건이 없으면 None."""
    details = _collect_details_for_date(target_date, monitored_laws, law_filter)

    if not details:
        if create_example_if_empty and monitored_laws:
            first = monitored_laws[0]
            meta = LawChangeMeta(
                law_name=first.name,
                category="법령",
                change_type="기타",
                announcement_date=target_date,
            )
            details.append(
                LawChangeDetail(
                    meta=meta,
                    reason_sections=["(예시) 개정이유 내용이 여기에 들어갑니다."],
                    main_change_sections=["(예시) 주요 개정사항 내용이 여기에 들어갑니다."],
                )
            )
        else:
            return None

    output_file = output_dir / f"law_change_guide_{target_date.strftime('%Y%m%d')}.docx"
    generate_guide(details, target_date, output_file, use_perplexity=use_perplexity)
    return output_file


def _process_legislation(
    output_dir: Path,
    monitored_laws: List[MonitoredLaw],
    law_filter: str,
    max_items: int = 30,
    use_perplexity: bool = True,
) -> Path | None:
    """입법예고/규정변경예고 (FSC) 조회·PDF 추출·안내서 생성."""
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
    for m in matches:
        meta = m.meta
        detail_url = meta.detail_url
        if not detail_url:
            continue
        # 개정이유·주요내용 = 게시글 본문(HTML)
        try:
            body_text = fetch_notice_body_text(detail_url)
        except Exception as e:
            print(f"[law_change_auto] 본문 조회 실패 ({meta.law_name[:30]}...): {e}")
            body_text = ""
        reason_sections, main_sections, opinion_deadline = parse_reason_main_from_notice_body(body_text)

        # 신구조문 대비표: 규정 고시안 PDF 다운로드·저장 후 Gemini 비전으로 표 추출
        try:
            comparison_pdfs = download_and_save_gosi_pdfs(detail_url, output_dir)
        except Exception as e:
            print(f"[law_change_auto] 첨부 PDF 저장 실패 ({meta.law_name[:30]}...): {e}")
            comparison_pdfs = []

        article_comparisons = []
        if comparison_pdfs:
            try:
                article_comparisons = extract_comparison_table_from_pdf_paths(comparison_pdfs)
            except Exception as e:
                print(f"[law_change_auto] PDF 대비표 추출 실패 (Gemini): {e}")

        combined = reason_sections + main_sections
        detail = LawChangeDetail(
            meta=meta,
            reason_sections=reason_sections,
            main_change_sections=main_sections,
            combined_reason_and_main_sections=combined,
            article_comparisons=article_comparisons,
            opinion_deadline=opinion_deadline,
            comparison_pdf_paths=comparison_pdfs,
        )
        details.append(detail)

    if not details:
        return None

    target_date = dt.date.today()
    notice_id = details[0].meta.law_id or "legislation"
    output_file = output_dir / f"law_change_guide_legislation_{notice_id}.docx"
    generate_guide(details, target_date, output_file, use_perplexity=use_perplexity)
    return output_file


def _collect_legislation_details(
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
    for m in matches:
        meta = m.meta
        detail_url = meta.detail_url
        if not detail_url:
            continue
        try:
            body_text = fetch_notice_body_text(detail_url)
        except Exception as e:
            print(f"[law_change_auto] 본문 조회 실패 ({meta.law_name[:30]}...): {e}")
            body_text = ""
        reason_sections, main_sections, opinion_deadline = parse_reason_main_from_notice_body(body_text)

        try:
            comparison_pdfs = download_and_save_gosi_pdfs(detail_url, output_dir)
        except Exception as e:
            print(f"[law_change_auto] 첨부 PDF 저장 실패 ({meta.law_name[:30]}...): {e}")
            comparison_pdfs = []

        article_comparisons = []
        if comparison_pdfs:
            try:
                article_comparisons = extract_comparison_table_from_pdf_paths(comparison_pdfs)
            except Exception as e:
                print(f"[law_change_auto] PDF 대비표 추출 실패 (Gemini): {e}")

        combined = reason_sections + main_sections
        detail = LawChangeDetail(
            meta=meta,
            reason_sections=reason_sections,
            main_change_sections=main_sections,
            combined_reason_and_main_sections=combined,
            article_comparisons=article_comparisons,
            opinion_deadline=opinion_deadline,
            comparison_pdf_paths=comparison_pdfs,
        )
        details.append(detail)

    return details


def _filename_for_detail(detail: LawChangeDetail, used: Set[str], max_base_len: int = 50) -> str:
    """법령 제목 기반 파일명 생성. 예: '보험업법 시행령 시행 안내.docx'"""
    meta = detail.meta
    if meta.category == "행정규칙":
        suffix = "고시 규정변경예고 안내"
    elif meta.category == "입법예고":
        suffix = "고시 규정변경예고 안내" if meta.change_type == "규정변경예고" else "입법예고 안내"
    else:
        suffix = "시행 안내"

    base = (meta.law_name or "법령").strip()
    base = re.sub(r"[\"*/:<>?\\|]", "_", base)
    base = re.sub(r"\s+", " ", base).strip()
    if len(base) > max_base_len:
        base = base[: max_base_len - 2].rstrip() + "…"
    name = f"{base} {suffix}.docx"
    if name in used:
        for i in range(2, 100):
            candidate = f"{base} {suffix}_{i}.docx"
            if candidate not in used:
                name = candidate
                break
    used.add(name)
    return name


def _process_comprehensive_period(
    output_dir: Path,
    monitored_laws: List[MonitoredLaw],
    law_filter: str,
    date_from: dt.date,
    date_to: dt.date,
    use_perplexity: bool = True,
) -> List[Path]:
    """기간 내 법령·행정규칙·입법예고를 건별로 안내서 1개씩 생성. (기간 API + 병렬로 단축)"""
    all_details: List[LawChangeDetail] = _collect_details_for_range(
        date_from, date_to, monitored_laws, law_filter
    )

    legis_details = _collect_legislation_details(
        output_dir, monitored_laws, law_filter, date_from, date_to
    )
    all_details.extend(legis_details)

    if not all_details:
        print("[law_change_auto] 해당 기간에 매칭되는 변경이 없습니다.")
        return []

    used_names: Set[str] = set()
    created: List[Path] = []
    for detail in all_details:
        filename = _filename_for_detail(detail, used_names)
        output_file = output_dir / filename
        generate_guide([detail], date_to, output_file, use_perplexity=use_perplexity)
        created.append(output_file)
    return created


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    output_dir = Path(args.output_dir)
    law_filter = (args.law or "").strip()

    monitored_laws: List[MonitoredLaw] = load_monitored_laws()
    print(f"[law_change_auto] 모니터링 대상 법령 수: {len(monitored_laws)}")
    print(f"[law_change_auto] 출력 폴더: {output_dir.resolve()}")

    if args.dry_run:
        for law in monitored_laws:
            print(f"  - {law.name}")
        print("[law_change_auto] dry-run 모드이므로 DOCX를 생성하지 않습니다.")
        return

    use_perplexity = not args.no_perplexity

    if args.legislation:
        print("[law_change_auto] 입법예고/규정변경예고 모드")
        result = _process_legislation(output_dir, monitored_laws, law_filter, use_perplexity=use_perplexity)
        if result:
            print(f"[law_change_auto] 안내서 생성 완료: {result.resolve()}")
        else:
            print("[law_change_auto] 매칭되는 입법예고가 없거나 PDF 추출에 실패했습니다.")
        return

    date_from_str = (args.date_from or "").strip()
    date_to_str = (args.date_to or "").strip()

    if date_from_str and date_to_str:
        # 종합 기간 모드: 법령·행정규칙·입법예고를 한 안내서로 통합
        date_from = _resolve_target_date(date_from_str)
        date_to = _resolve_target_date(date_to_str)
        if date_from > date_to:
            print(f"[law_change_auto] 오류: --date-from({date_from})이 --date-to({date_to})보다 늦습니다.")
            return
        total_days = (date_to - date_from).days + 1
        print(f"[law_change_auto] 종합 기간 모드: {date_from.isoformat()} ~ {date_to.isoformat()} ({total_days}일)")
        print("[law_change_auto] 법령·시행령·행정규칙·입법예고 건별 안내서 생성 중...")

        created = _process_comprehensive_period(
            output_dir, monitored_laws, law_filter, date_from, date_to, use_perplexity=use_perplexity
        )
        if created:
            for path in created:
                print(f"[law_change_auto] 생성: {path.name}")
            print(f"[law_change_auto] 총 {len(created)}개 파일 생성 완료")
        else:
            print("[law_change_auto] 해당 기간에 매칭되는 변경이 없습니다.")
    else:
        # 단일 날짜 모드
        target_date = _resolve_target_date(args.date)
        print(f"[law_change_auto] 기준일자: {target_date.isoformat()}")

        result = _process_single_date(
            target_date, output_dir, monitored_laws, law_filter,
            create_example_if_empty=True, use_perplexity=use_perplexity,
        )
        if result:
            print(f"[law_change_auto] 안내서 생성 완료: {result.resolve()}")
        else:
            print("[law_change_auto] 해당 일자에 매칭되는 변경이 없습니다.")


if __name__ == "__main__":
    main()
