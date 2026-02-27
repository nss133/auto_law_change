from __future__ import annotations

import argparse
import datetime as dt
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
        help="기준 일자 (예: 2026-02-26, 기본값: today)",
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


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    target_date = _resolve_target_date(args.date)
    output_dir = Path(args.output_dir)

    print(f"[law_change_auto] 기준일자: {target_date.isoformat()}")
    print(f"[law_change_auto] 출력 폴더: {output_dir.resolve()}")

    # 1. 모니터링 대상 법령 리스트 로드
    monitored_laws: List[MonitoredLaw] = load_monitored_laws()
    print(f"[law_change_auto] 모니터링 대상 법령 수: {len(monitored_laws)}")

    if args.dry_run:
        for law in monitored_laws:
            print(f"  - {law.name}")
        print("[law_change_auto] dry-run 모드이므로 DOCX를 생성하지 않습니다.")
        return

    # 2. 국가법령정보센터 Open API에서 변경 목록 조회 (현재는 fetcher가 비어 있어 항상 0건일 수 있음)
    try:
        law_metas: List[LawChangeMeta] = get_law_changes_for_monitored(
            monitored_laws, target_date
        )
        admin_rule_metas: List[LawChangeMeta] = get_recent_admin_rule_changes(target_date)
    except Exception as e:
        print(f"[law_change_auto] 경고: Open API 호출 중 오류 발생: {e}")
        law_metas = []
        admin_rule_metas = []

    all_metas: List[LawChangeMeta] = [*law_metas, *admin_rule_metas]
    print(f"[law_change_auto] 수집된 원천 변경 건수: {len(all_metas)}")

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

    # 3. 모니터링 대상과 유사도 기반 매칭
    matches: List[MatchResult] = match_laws(monitored_laws, all_metas, threshold=0.8)

    # --law 옵션이 있으면 해당 문자열이 포함된 법령명만 필터링
    law_filter = (args.law or "").strip()
    if law_filter:
        filtered: List[MatchResult] = [
            m for m in matches if law_filter in m.meta.law_name
        ]
        print(
            f"[law_change_auto] --law='{law_filter}' 조건에 맞는 매칭 건수: {len(filtered)} "
            f"(전체 매칭 {len(matches)}건 중)"
        )
        matches = filtered
    else:
        print(f"[law_change_auto] 모니터링 리스트와 매칭된 건수: {len(matches)}")

    details: List[LawChangeDetail] = []

    # 매칭된 건이 있으면 개정이유/신구법비교 XML을 가져와 파싱
    for m in matches:
        meta = m.meta
        revision_html: str | None = None
        revision_text_from_list: str | None = None
        old_new_xml: str | None = None

        if meta.law_type == "ls" and meta.law_id and meta.effective_date:
            date_str = f"{meta.effective_date.year}. {meta.effective_date.month}. {meta.effective_date.day}."
            chr_cls_cd = meta.chr_cls_cd or "010202"
            try:
                text, display_meta = fetch_revision_reason_from_ls_rvs_rsn_list(
                    meta.law_id, chr_cls_cd, date_str
                )
                if text:
                    revision_text_from_list = text
                if display_meta:
                    meta.law_number = display_meta.get("law_number")
                    meta.amendment_date_str = display_meta.get("amendment_date_str")
                    meta.amendment_type = display_meta.get("amendment_type")
            except Exception as e:
                print(f"[law_change_auto] lsRvsRsnListP 개정이유 조회 실패: {meta.law_name}: {e}")

        if not revision_text_from_list:
            try:
                revision_html = fetch_revision_html(meta)
            except Exception as e:
                print(f"[law_change_auto] 개정이유 조회 실패: {meta.law_name}: {e}")

        try:
            old_new_xml = fetch_old_new_html(meta)
        except Exception as e:
            print(f"[law_change_auto] 신구법비교 조회 실패: {meta.law_name}: {e}")

        detail = parse_law_change(
            meta, revision_html, old_new_xml, revision_text_from_list=revision_text_from_list
        )
        details.append(detail)

    # 매칭/파싱된 내용이 하나도 없으면 예시 데이터로 기본 구조만 생성
    if not details and monitored_laws:
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

    output_file = output_dir / f"law_change_guide_{target_date.strftime('%Y%m%d')}.docx"
    generate_guide(details, target_date, output_file)
    print(f"[law_change_auto] 안내서 생성 완료: {output_file.resolve()}")


if __name__ == "__main__":
    main()

