from __future__ import annotations

import argparse
import datetime as dt
import re
import requests
from datetime import date
from pathlib import Path
from typing import List, Set, Tuple

from dotenv import load_dotenv

# 프로젝트 루트의 .env에서 LAW_GO_API_KEY, GEMINI_API_KEY 등 로드
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from .config.monitored_laws_loader import MonitoredLaw, load_monitored_laws
from .docx_generator.generator import generate_guide, guide_display_title, write_period_toc_docx
from .models import LawChangeDetail, LawChangeMeta
from .services.pipeline import (
    collect_details_for_date,
    collect_details_for_range,
    collect_legislation_details,
    process_legislation,
)


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
        "--no-web-check",
        action="store_true",
        help="웹 스크래핑 기반 교차검증을 비활성화합니다.",
    )
    return parser.parse_args(argv)


def _resolve_target_date(value: str) -> dt.date:
    if value == "today":
        return dt.date.today()
    return dt.date.fromisoformat(value)


def _process_single_date(
    target_date: dt.date,
    output_dir: Path,
    monitored_laws: List[MonitoredLaw],
    law_filter: str,
    create_example_if_empty: bool = True,
    no_web_check: bool = False,
) -> List[Path]:
    """단일 날짜에 대한 변경 조회·파싱·DOCX 생성. `목차.docx` 및 `N. {법령명} … 안내.docx` 목록."""
    details = collect_details_for_date(
        target_date, monitored_laws, law_filter, no_web_check=no_web_check
    )

    # 금융위 입법예고·규정변경예고: --date-from/--date-to 기간 모드와 같이 예고일(게시일)이 해당 일자인 건만 병합
    try:
        legis_details = collect_legislation_details(
            output_dir, monitored_laws, law_filter, target_date, target_date
        )
        details.extend(legis_details)
    except Exception as e:
        print(f"[law_change_auto] 입법예고 수집 중 오류(무시하고 계속): {e}")

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
            return []

    period_line = f"{target_date.year}. {target_date.month}. {target_date.day}."
    created = _write_guides_numbered_with_toc(
        details,
        output_dir,
        guide_date=target_date,
        period_line=period_line,
        sort_fallback=target_date,
    )
    _download_legislation_notice_pdf_attachments(details, output_dir)
    return created


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


def _detail_sort_date_for_toc(detail: LawChangeDetail, fallback: dt.date) -> dt.date:
    """목차·정렬용 기준일: 예고일·공포 등 announcement_date 우선, 없으면 시행일, 없으면 기간 종료일."""
    m = detail.meta
    return m.announcement_date or m.effective_date or fallback


def _sort_details_for_period_toc(details: List[LawChangeDetail], fallback_date: dt.date) -> List[LawChangeDetail]:
    """날짜 오름차순, 같은 날은 법령명 가나다순(가능하면 로캘 strxfrm)."""
    import locale

    try:
        locale.setlocale(locale.LC_COLLATE, "ko_KR.UTF-8")
    except Exception:
        try:
            locale.setlocale(locale.LC_COLLATE, "Korean_Korea.UTF-8")
        except Exception:
            pass

    def _name_key(name: str) -> str:
        try:
            return locale.strxfrm(name or "")
        except Exception:
            return name or ""

    return sorted(
        details,
        key=lambda d: (_detail_sort_date_for_toc(d, fallback_date), _name_key(d.meta.law_name or "")),
    )


def _write_guides_numbered_with_toc(
    details: List[LawChangeDetail],
    output_dir: Path,
    *,
    guide_date: dt.date,
    period_line: str,
    sort_fallback: dt.date,
) -> List[Path]:
    """건당 `N. {법령명} … 안내.docx` + `목차.docx`. 단일 일자·기간 모드 공통."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    sorted_details = _sort_details_for_period_toc(details, sort_fallback)
    used_names: Set[str] = set()
    created: List[Path] = []
    toc_pairs: List[Tuple[LawChangeDetail, Path]] = []

    for i, detail in enumerate(sorted_details, start=1):
        filename = _filename_for_detail(detail, used_names)
        numbered_filename = f"{i}. {filename}"
        output_file = output_dir / numbered_filename
        generate_guide([detail], guide_date, output_file)
        created.append(output_file)
        toc_pairs.append((detail, output_file))

    toc_lines: List[str] = []
    for i, (detail, out_path) in enumerate(toc_pairs, start=1):
        sd = _detail_sort_date_for_toc(detail, sort_fallback)
        date_fmt = f"{sd.year}. {sd.month}. {sd.day}."
        title = guide_display_title(detail.meta)
        toc_lines.append(f"{i}. {date_fmt} {title} ({out_path.name})")

    toc_path = output_dir / "목차.docx"
    write_period_toc_docx(toc_path, period_line, toc_lines)
    created.insert(0, toc_path)
    return created


def _download_legislation_notice_pdf_attachments(
    details: List[LawChangeDetail], output_dir: Path
) -> None:
    """입법예고 법령안 첨부(PDF·HWP·HWPX)를 안내서 폴더에 저장."""
    _doc_exts = (".pdf", ".hwp", ".hwpx")

    for detail in details:
        if detail.meta.category != "입법예고" or not detail.attachments:
            continue
        for att in detail.attachments:
            att_name = att.get("name", "")
            att_url = att.get("url", "")
            if not att_name or not att_url:
                continue
            url_lower = att_url.lower()
            if not any(url_lower.endswith(ext) or ext in url_lower for ext in _doc_exts):
                continue
            if "법령안" not in att_name and "법령 안" not in att_name:
                continue
            safe_att = re.sub(r'[\\/:*?"<>|]', "_", att_name)
            # 확장자 보정
            if not any(safe_att.lower().endswith(ext) for ext in _doc_exts):
                safe_att += ".pdf"
            att_path = output_dir / safe_att
            try:
                resp = requests.get(att_url, timeout=30)
                resp.raise_for_status()
                att_path.write_bytes(resp.content)
                print(f"    📎 {safe_att}")
            except Exception as e:
                print(f"[law_change_auto] 첨부파일 다운로드 실패: {att_name}: {e}")


def _process_comprehensive_period(
    output_dir: Path,
    monitored_laws: List[MonitoredLaw],
    law_filter: str,
    date_from: dt.date,
    date_to: dt.date,
) -> List[Path]:
    """기간 내 법령·행정규칙·입법예고를 건별로 안내서 1개씩 생성. (기간 API + 병렬로 단축)"""
    all_details: List[LawChangeDetail] = collect_details_for_range(
        date_from, date_to, monitored_laws, law_filter
    )

    legis_details = collect_legislation_details(
        output_dir, monitored_laws, law_filter, date_from, date_to
    )
    all_details.extend(legis_details)

    # 중복 제거: collect_details_for_range와 collect_legislation_details 양쪽에서 FSC를 수집하므로
    # 중복 발견 시 comparison_pdf_paths가 있는 버전의 PDF 정보를 기존 버전에 보완(머지)
    seen_seqs: dict[str, int] = {}  # seq_key → deduped index
    deduped: List[LawChangeDetail] = []
    for d in all_details:
        seq_key = (
            d.meta.lsi_seq
            or d.meta.admrul_seq
            or d.meta.law_id
            or f"{d.meta.law_name}_{d.meta.announcement_date}"
        )
        if seq_key in seen_seqs:
            existing = deduped[seen_seqs[seq_key]]
            # 이미 저장된 버전에 PDF paths 보완
            if d.comparison_pdf_paths and not existing.comparison_pdf_paths:
                existing.comparison_pdf_paths = d.comparison_pdf_paths
            # 이미 저장된 버전에 신구조문 rows 보완
            if d.article_comparisons and not existing.article_comparisons:
                existing.article_comparisons = d.article_comparisons
            continue
        seen_seqs[seq_key] = len(deduped)
        deduped.append(d)
    if len(deduped) < len(all_details):
        print(f"[law_change_auto] 중복 제거: {len(all_details)}건 → {len(deduped)}건")
    all_details = deduped

    if not all_details:
        print("[law_change_auto] 해당 기간에 매칭되는 변경이 없습니다.")
        return []

    period_line = (
        f"{date_from.year}. {date_from.month}. {date_from.day}. "
        f"~ {date_to.year}. {date_to.month}. {date_to.day}."
    )
    created = _write_guides_numbered_with_toc(
        all_details,
        output_dir,
        guide_date=date_to,
        period_line=period_line,
        sort_fallback=date_to,
    )
    _download_legislation_notice_pdf_attachments(all_details, output_dir)
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

    from law_change_auto.services import gemini_client as _gmod

    if not _gmod._get_gemini_key() and not _gmod._get_groq_key():
        print(
            "[law_change_auto] 경고: GEMINI_API_KEY / GROQ_API_KEY가 없습니다. 파급효과는 기본 문구만 넣습니다.",
            flush=True,
        )

    if args.legislation:
        print("[law_change_auto] 입법예고/규정변경예고 모드")
        result = process_legislation(output_dir, monitored_laws, law_filter)
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
            output_dir, monitored_laws, law_filter, date_from, date_to
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

        created = _process_single_date(
            target_date,
            output_dir,
            monitored_laws,
            law_filter,
            create_example_if_empty=True,
            no_web_check=args.no_web_check,
        )
        if created:
            for path in created:
                print(f"[law_change_auto] 생성: {path.name}")
            print(f"[law_change_auto] 안내서 생성 완료 ({len(created)}개)")
        else:
            print("[law_change_auto] 해당 일자에 매칭되는 변경이 없습니다.")


if __name__ == "__main__":
    main()
