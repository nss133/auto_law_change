from __future__ import annotations

import argparse
import datetime as dt
import time
from pathlib import Path
from typing import List

from .config.monitored_laws_loader import MonitoredLaw, load_monitored_laws
from .docx_generator.generator import generate_guide
from .fetchers.national_law_fetcher import (
    get_law_changes_for_monitored,
    get_recent_admin_rule_changes,
)
from .fetchers.content_fetcher import (
    fetch_old_new_html,
    fetch_revision_html,
    fetch_revision_reason_from_ls_rvs_rsn_list,
)
from .matching.law_matcher import MatchResult, match_laws
from .models import LawChangeDetail, LawChangeMeta
from .parsers.law_change_parser import parse_law_change


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


def _process_single_date(
    target_date: dt.date,
    output_dir: Path,
    monitored_laws: List[MonitoredLaw],
    law_filter: str,
    create_example_if_empty: bool = True,
) -> Path | None:
    """단일 날짜에 대한 변경 조회·파싱·DOCX 생성. 생성된 파일 경로를 반환하고, 건이 없으면 None."""
    law_metas: List[LawChangeMeta] = []
    admin_rule_metas: List[LawChangeMeta] = []

    try:
        law_metas = get_law_changes_for_monitored(monitored_laws, target_date)
        admin_rule_metas = get_recent_admin_rule_changes(target_date)
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
    generate_guide(details, target_date, output_file)
    return output_file


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

    date_from_str = (args.date_from or "").strip()
    date_to_str = (args.date_to or "").strip()

    if date_from_str and date_to_str:
        # 기간 모드: date_from ~ date_to 각 날짜별 DOCX 생성
        date_from = _resolve_target_date(date_from_str)
        date_to = _resolve_target_date(date_to_str)
        if date_from > date_to:
            print(f"[law_change_auto] 오류: --date-from({date_from})이 --date-to({date_to})보다 늦습니다.")
            return
        total_days = (date_to - date_from).days + 1
        print(f"[law_change_auto] 기간 모드: {date_from.isoformat()} ~ {date_to.isoformat()} ({total_days}일)")

        created: List[Path] = []
        for d in _date_range(date_from, date_to):
            result = _process_single_date(
                d, output_dir, monitored_laws, law_filter, create_example_if_empty=False
            )
            if result:
                created.append(result)
                print(f"[law_change_auto] 생성: {result.name}")
            # 매칭 없는 날은 조용히 스킵
            time.sleep(1.5)  # API 호출 간격 완화

        print(f"[law_change_auto] 기간 처리 완료: 총 {len(created)}개 파일 생성")
    else:
        # 단일 날짜 모드
        target_date = _resolve_target_date(args.date)
        print(f"[law_change_auto] 기준일자: {target_date.isoformat()}")

        result = _process_single_date(
            target_date, output_dir, monitored_laws, law_filter, create_example_if_empty=True
        )
        if result:
            print(f"[law_change_auto] 안내서 생성 완료: {result.resolve()}")
        else:
            print("[law_change_auto] 해당 일자에 매칭되는 변경이 없습니다.")


if __name__ == "__main__":
    main()
