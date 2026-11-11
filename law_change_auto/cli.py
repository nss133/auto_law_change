from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import re
import requests
import sys
import time
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
    policy = parser.add_mutually_exclusive_group()
    policy.add_argument('--detailed-guides', action='store_true',
                        help='시행 법령군별로 묶는 기존 방식(명시 선택). 기본은 개별본+목차+요약.')
    policy.add_argument('--digest-only', action='store_true', help='개별 안내서 없이 요약만 생성(명시 선택).')
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
        help="기간 시작일 (예: 2025-10-01). --date-to와 함께 사용 시 개별본·목차·종합요약 생성.",
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
        "--list-monitored",
        action="store_true",
        help="모니터링 대상 법령 목록만 출력하고 종료합니다.",
    )
    parser.add_argument(
        "--validate-env",
        action="store_true",
        help="필수 파일·환경변수·Python 의존성 상태를 점검하고 종료합니다.",
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
    parser.add_argument(
        "--no-summary",
        action="store_true",
        help="법령제·개정 주요내용 요약 docx 생성을 비활성화합니다.",
    )
    parser.add_argument(
        "--no-summary-llm",
        action="store_true",
        help="요약은 본문 추출로 생성. 기간 모드에서는 상세 파급효과 AI 호출도 생략합니다.",
    )
    parser.add_argument('--selection-review', type=Path,
                        help='원문 해시에 결합된 후보별 선별 검토 JSON. 자동 분류의 적용 조건을 확인한 뒤 사용.')
    args = parser.parse_args(argv)
    if bool(args.date_from) != bool(args.date_to):
        parser.error('--date-from과 --date-to를 함께 지정해야 함. 단일 일자로 대신 실행하지 않음.')
    if (args.digest_only or args.detailed_guides) and not args.date_from:
        parser.error('--digest-only/--detailed-guides는 기간 모드에서만 사용함.')
    return args


def _resolve_target_date(value: str) -> dt.date:
    if value == "today":
        return dt.date.today()
    return dt.date.fromisoformat(value)


def _validate_environment(monitored_laws: List[MonitoredLaw]) -> int:
    """로컬 실행 전 점검. 네트워크 호출 없이 구성 상태만 확인한다."""
    checks: list[tuple[str, bool, str]] = []
    from .docx_generator.reference import reference_path
    try:
        reference_path()
        checks.append(('지정 DOCX 양식·해시', True, '보존 템플릿 일치'))
    except (OSError, ValueError) as exc:
        checks.append(('지정 DOCX 양식·해시', False, str(exc)))

    checks.append(
        (
            "Python 3.9 이상 (3.10+ 권장)",
            sys.version_info >= (3, 9),
            f"현재 {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        )
    )
    checks.append(
        (
            "모니터링 엑셀",
            Path("data/monitored_laws.xlsx").exists() and bool(monitored_laws),
            f"{len(monitored_laws)}건 로드",
        )
    )
    checks.append(
        (
            "LAW_GO_API_KEY",
            bool(os.getenv("LAW_GO_API_KEY", "").strip()),
            "법령 API 조회에 필요",
        )
    )
    checks.append(
        (
            "GEMINI_API_KEY 또는 GROQ_API_KEY",
            bool(
                os.getenv("GEMINI_API_KEY", "").strip()
                or os.getenv("GROQ_API_KEY", "").strip()
            ),
            "없으면 파급효과 생성 보류·배포 불가",
        )
    )

    required_modules = [
        "requests",
        "bs4",
        "dotenv",
        "lxml",
        "docx",
        "pandas",
        "openpyxl",
        "Levenshtein",
        "fitz",
        "pdfplumber",
    ]
    for module_name in required_modules:
        checks.append(
            (
                f"Python 모듈: {module_name}",
                importlib.util.find_spec(module_name) is not None,
                "requirements.txt",
            )
        )

    print("[law_change_auto] 환경 점검")
    failed = 0
    for label, ok, note in checks:
        mark = "OK" if ok else "FAIL"
        print(f"  [{mark}] {label} - {note}")
        if not ok:
            failed += 1

    if failed:
        print(f"[law_change_auto] 점검 실패 {failed}건. 위 항목을 보완해 주세요.")
        return 1
    print("[law_change_auto] 환경 점검 통과")
    return 0


def _date_to_iso(value: dt.date | None) -> str | None:
    return value.isoformat() if value else None


def _detail_report_item(detail: LawChangeDetail, output_file: Path | None = None) -> dict:
    meta = detail.meta
    return {
        "law_name": meta.law_name,
        "category": meta.category,
        "change_type": meta.change_type,
        "announcement_date": _date_to_iso(meta.announcement_date),
        "effective_date": _date_to_iso(meta.effective_date),
        "source": meta.source,
        "detail_url": meta.detail_url,
        "amendment_type": meta.amendment_type,
        "law_id": meta.law_id,
        "lsi_seq": meta.lsi_seq,
        "admrul_seq": meta.admrul_seq,
        "calendar_event_date": _date_to_iso(meta.calendar_event_date),
        "opinion_deadline": detail.opinion_deadline,
        "reason_count": len(detail.reason_sections),
        "main_change_count": len(detail.main_change_sections),
        "comparison_count": len(detail.article_comparisons),
        "attachment_count": len(detail.attachments),
        "output_file": output_file.name if output_file else None,
    }


def _write_run_report(
    output_dir: Path,
    *,
    mode: str,
    period_line: str,
    details: List[LawChangeDetail],
    created: List[Path],
    guide_date: dt.date,
) -> Path:
    """실행 결과를 JSON으로 남겨 누락·매칭·산출물 추적을 쉽게 한다."""
    detail_files = [path for path in created if path.name != "목차.docx"]
    report = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "mode": mode,
        "period": period_line,
        "guide_date": guide_date.isoformat(),
        "summary": {
            "detail_count": len(details),
            "created_file_count": len(created),
            "legislation_count": sum(1 for d in details if d.meta.category == "입법예고"),
            "admin_rule_count": sum(1 for d in details if d.meta.category == "행정규칙"),
            "law_count": sum(1 for d in details if d.meta.category == "법령"),
        },
        "files": [str(path.resolve()) for path in created],
        "details": [
            _detail_report_item(
                detail,
                detail_files[i] if i < len(detail_files) else None,
            )
            for i, detail in enumerate(details)
        ],
    }
    report_path = output_dir / "run_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[law_change_auto] 실행 리포트: {report_path.name}")
    from .services.delivery import write_manifest
    write_manifest(output_dir, details, created,
                   {id(d): detail_files[i] for i, d in enumerate(details) if i < len(detail_files)}, report)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report_path


def _process_single_date(
    target_date: dt.date,
    output_dir: Path,
    monitored_laws: List[MonitoredLaw],
    law_filter: str,
    create_example_if_empty: bool = False,
    no_web_check: bool = False,
    make_summary: bool = True,
    summary_use_llm: bool = True,
    selection_review=None,
) -> List[Path]:
    """단일 날짜에 대한 변경 조회·파싱·DOCX 생성. `목차.docx` 및 `N. {법령명} … 안내.docx` 목록."""
    from .services import diagnostics
    diagnostics.reset()
    if any(Path(output_dir).glob('*.docx')) or (Path(output_dir) / 'run_report.json').exists():
        raise FileExistsError(f'기존 산출물 보존. 새 --output-dir을 지정하세요: {output_dir}')
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
        mode="single-date",
        make_summary=make_summary,
        summary_use_llm=summary_use_llm,
        selection_review=selection_review,
    )
    _download_legislation_notice_pdf_attachments(details, output_dir)
    return created


def _filename_for_detail(detail: LawChangeDetail, used: Set[str], max_base_len: int = 50) -> str:
    """법령 제목 기반 파일명 생성. 예: '보험업법 시행령 시행 안내.docx'"""
    from .services.publication import safe_filename
    return safe_filename(guide_display_title(detail.meta), used)


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
    mode: str = "guide",
    make_summary: bool = True,
    summary_use_llm: bool = True,
    issue_date: dt.date | None = None,
    selection_review=None,
) -> List[Path]:
    """건당 `N. {법령명} … 안내.docx` + `목차.docx`. 단일 일자·기간 모드 공통.

    issue_date: 안내서·요약에 찍히는 발행월(법무팀 발행 기준). 기간 모드에서는 시작월을
    쓰며, 미지정 시 guide_date를 사용한다. (시행일 등 본문 날짜와는 무관)
    """
    return _publish_period(details, output_dir, guide_date, guide_date, period_line,
                           make_summary=make_summary, use_llm=summary_use_llm,
                           selection_review=selection_review, mode=mode)


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
    *,
    make_summary: bool = True,
    summary_use_llm: bool = True,
    detailed_guides: bool = False,
    digest_only: bool = False,
    selection_review=None,
) -> List[Path]:
    """기간 수집 후 개별 안내서·목차·요약 초안 생성. 다른 정책은 명시 선택함."""
    from .services import diagnostics
    diagnostics.reset()
    if any(Path(output_dir).glob('*.docx')) or (Path(output_dir) / 'run_report.json').exists():
        raise FileExistsError(f'기존 산출물을 보존합니다. 새 --output-dir을 지정하세요: {output_dir}')
    started = time.perf_counter()
    all_details: List[LawChangeDetail] = collect_details_for_range(
        date_from, date_to, monitored_laws, law_filter
    )

    collected = time.perf_counter()
    legis_details = collect_legislation_details(
        output_dir, monitored_laws, law_filter, date_from, date_to, extract_comparisons=True,
        comparison_llm=False
    )
    all_details.extend(legis_details)

    # 출처 유형 + 개정 버전 기준 중복 제거; 다른 개정 버전은 유지한다.
    seen_seqs: dict[tuple, int] = {}
    deduped: List[LawChangeDetail] = []
    for d in all_details:
        seq_key = (d.meta.category, d.meta.lsi_seq or d.meta.admrul_seq
                   or (d.meta.law_id, d.meta.law_name, d.meta.announcement_date, d.meta.effective_date))
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

    period_line = (
        f"{date_from.year}. {date_from.month}. {date_from.day}. "
        f"~ {date_to.year}. {date_to.month}. {date_to.day}."
    )
    return _publish_period(all_details, output_dir, date_from, date_to, period_line,
                           detailed_guides=detailed_guides, digest_only=digest_only, make_summary=make_summary,
                           use_llm=summary_use_llm, selection_review=selection_review, timings={
                               'law_collection_seconds': round(collected-started, 2),
                               'notice_collection_seconds': round(time.perf_counter()-collected, 2)})


def _publish_period(details, output_dir, start, end, period_line, *,
                    detailed_guides=False, digest_only=False, make_summary=True, use_llm=True, timings=None,
                    selection_review=None, mode='period', reuse_verified=False,
                    enrich_candidate_ids=None, progress=None):
    from .services.publication import classify, group_effective, safe_filename
    from .services.enrichment import enrich, source_limit, mark_source_conflicts, reusable_draft
    from .services.diagnostics import snapshot
    from .docx_generator.summary_generator import generate_summary_docx
    from .services.selection import select_candidates, write_registry, SELECTION_POLICY, candidate_id
    begun = time.perf_counter()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    # Reusing a directory would leave stale individual guides beside the new digest.
    if any(output_dir.glob('*.docx')) or (output_dir / 'run_report.json').exists():
        raise FileExistsError(f'기존 산출물을 보존합니다. 새 --output-dir을 지정하세요: {output_dir}')
    from dataclasses import asdict
    status_order = {'시행': 0, '향후 시행': 1, '공포·발령': 2, '예고': 3, '별도 확인': 4}
    details = sorted(details, key=lambda d: (status_order[classify(d, start, end)],
        (d.meta.effective_date if classify(d, start, end) == '시행' else
         d.meta.announcement_date if classify(d, start, end) != '별도 확인' else
         d.meta.calendar_event_date) or end, d.meta.law_name, d.meta.lsi_seq or ''))
    mark_source_conflicts(details)
    for d in details:
        d.publication_status = classify(d, start, end)
    (output_dir / 'collected_details.json').write_text(
        json.dumps([asdict(d) for d in details], ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    candidates = details
    details = select_candidates(candidates, selection_review)
    for d in candidates:
        if d.selection['status'] != 'priority':
            d.summary_text = None
            d.impact_analysis = None
            d.impact_correction = {}
            d.impact_evidence = {'status': 'not_selected'}
    groups = group_effective(details, start, end, load_monitored_laws())
    file_map, created, used = {}, [], set()
    hits = 0
    individual = not digest_only and not detailed_guides
    if digest_only and detailed_guides:
        raise ValueError('digest-only와 법령군별 상세본은 동시에 선택할 수 없음')
    for i, d in enumerate(details, 1):
        if progress:
            progress(f'초안 확인 {i}/{len(details)} · {d.meta.law_name}')
        d.publication_status = classify(d, start, end)
        limit = source_limit(d)
        if limit:
            d.summary_text = limit
            d.impact_analysis = None
            d.impact_evidence = {'status': 'withheld_source_limit', 'reason': limit}
            continue
        needs_impact = individual or detailed_guides and d.publication_status == '시행'
        targeted = enrich_candidate_ids is None or candidate_id(d) in enrich_candidate_ids
        if reuse_verified and reusable_draft(d, impact=needs_impact):
            d.impact_evidence = {**d.impact_evidence, 'reused_response': True, 'cache_hit': False}
        elif use_llm and targeted and (make_summary or needs_impact):
            print(f'[law_change_auto] 초안 {i}/{len(details)}: {d.meta.law_name}', flush=True)
            hits += enrich(d, output_dir.parent / '.draft_cache', impact=needs_impact)
        else:
            d.summary_text = None
            d.impact_analysis = None
            d.impact_correction = {}
            d.impact_evidence = {'status': 'not_retried' if use_llm and not targeted else 'disabled'}
    enriched = time.perf_counter()
    if detailed_guides:
        for i, ds in enumerate(groups.values(), 1):
            name = re.sub(r'\s*(시행령|시행규칙)$', '', ds[0].meta.law_name)
            path = output_dir / safe_filename(f'{i}. {name} 관련 시행 안내', used)
            generate_guide(ds, dt.date.today(), path)
            created.append(path)
            for d in ds:
                file_map[id(d)] = path
    if individual and details:
        toc_lines = []
        for i, d in enumerate(details, 1):
            path = output_dir / safe_filename(f'{i:02d}. {guide_display_title(d.meta)}', used)
            generate_guide([d], start, path)
            created.append(path)
            file_map[id(d)] = path
            toc_lines.append(f'{i:02d}. [{d.publication_status}] {d.meta.law_name}')
        toc = output_dir / '목차.docx'
        write_period_toc_docx(toc, period_line, toc_lines)
        created.append(toc)
    if make_summary and details:
        path = output_dir / '법령제개정_주요내용_요약.docx'
        generate_summary_docx(details, path, period_line=period_line, guide_date=end,
                              issue_date=start, use_llm=False)
        created.append(path)
    registry = write_registry(output_dir, candidates, file_map)
    timings = dict(timings or {})
    timings.update(enrichment_seconds=round(enriched-begun, 2),
                   document_seconds=round(time.perf_counter()-enriched, 2))
    timings['total_seconds'] = round(sum(timings.values()), 2)
    report = {
        'generated_at': dt.datetime.now().isoformat(timespec='seconds'),
        'as_of': dt.date.today().isoformat(), 'mode': mode, 'period': period_line,
        'date_from': start.isoformat(), 'date_to': end.isoformat(),
        'selection_policy': SELECTION_POLICY, 'selection_counts': registry['counts'],
        'publication_policy': 'effective-family' if detailed_guides else 'digest-only' if digest_only else 'individual-and-digest',
        'delivery_status': 'pending_review',
        'coverage_status': 'incomplete' if snapshot() else 'no_reported_errors',
        'warnings': snapshot(), 'timings': timings, 'draft_cache_hits': hits,
        'summary': {'detail_count': len(candidates), 'selected_count': len(details), 'effective_family_count': len(groups),
                    'created_file_count': len(created),
                    'individual_guide_count': len(set(file_map.values()))},
        'files': [str(p.resolve()) for p in created],
        'details': [{**_detail_report_item(d, file_map.get(id(d))),
                     'publication_status': d.publication_status,
                     'impact_evidence': d.impact_evidence, 'source_limit': source_limit(d),
                     'selection': d.selection} for d in candidates],
    }
    (output_dir / 'published_details.json').write_text(
        json.dumps([asdict(d) for d in details], ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    from .services.delivery import write_manifest
    write_manifest(output_dir, details, created, file_map, report)
    (output_dir / 'run_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'[law_change_auto] 탐지 {len(candidates)}건 / 우선 안내 {len(details)}건 / DOCX {len(created)}개 / 경고 {len(snapshot())}건', flush=True)
    return created


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(line_buffering=True)
    args = parse_args(argv)
    output_dir = Path(args.output_dir)
    law_filter = (args.law or "").strip()

    monitored_laws: List[MonitoredLaw] = load_monitored_laws()
    print(f"[law_change_auto] 모니터링 대상 법령 수: {len(monitored_laws)}")
    print(f"[law_change_auto] 출력 폴더: {output_dir.resolve()}")

    if args.validate_env:
        raise SystemExit(_validate_environment(monitored_laws))

    if args.list_monitored or args.dry_run:
        for law in monitored_laws:
            print(f"  - {law.name}")
        if args.list_monitored:
            return
        print("[law_change_auto] dry-run 모드이므로 DOCX를 생성하지 않습니다.")
        return

    from law_change_auto.services import gemini_client as _gmod

    _gmod.reset_gemini_circuit()

    if not _gmod._get_gemini_key() and not _gmod._get_groq_key():
        print(
            "[law_change_auto] 경고: GEMINI_API_KEY / GROQ_API_KEY가 없습니다. 파급효과를 생성 보류하고 배포 불가로 기록합니다.",
            flush=True,
        )

    if args.legislation:
        print("[law_change_auto] 입법예고/규정변경예고 모드")
        result = process_legislation(output_dir, monitored_laws, law_filter,
                                     selection_review=args.selection_review, use_llm=not args.no_summary_llm,
                                     make_summary=not args.no_summary)
        if result:
            print(f"[law_change_auto] 선별 안내서 초안 {len(result)}개 생성; 배포 전 검증 필요")
        else:
            print("[law_change_auto] 우선 안내 예고가 없습니다. 수집 결과와 후보 선별 검토 목록을 확인하세요.")
        return

    date_from_str = (args.date_from or "").strip()
    date_to_str = (args.date_to or "").strip()

    if date_from_str and date_to_str:
        # 기간 모드: 개별 안내서·목차·요약을 기본 생성
        date_from = _resolve_target_date(date_from_str)
        date_to = _resolve_target_date(date_to_str)
        if date_from > date_to:
            print(f"[law_change_auto] 오류: --date-from({date_from})이 --date-to({date_to})보다 늦습니다.")
            return
        total_days = (date_to - date_from).days + 1
        print(f"[law_change_auto] 종합 기간 모드: {date_from.isoformat()} ~ {date_to.isoformat()} ({total_days}일)")
        print("[law_change_auto] 개별 안내서·목차·요약 초안 생성 중 (요약만: --digest-only)...")

        created = _process_comprehensive_period(
            output_dir, monitored_laws, law_filter, date_from, date_to,
            make_summary=not args.no_summary,
            summary_use_llm=not args.no_summary_llm,
            detailed_guides=args.detailed_guides,
            digest_only=args.digest_only,
            selection_review=args.selection_review,
        )
        if created:
            for path in created:
                print(f"[law_change_auto] 생성: {path.name}")
            print(f"[law_change_auto] 초안 {len(created)}개 생성. 배포 전 delivery_manifest.json 검증·원문 대조·전 페이지 검수 필요")
        else:
            from .services.diagnostics import snapshot
            print("[law_change_auto] 생성된 DOCX 없음. 수집 경고와 후보 건수는 run_report.json을 확인하세요."
                  if snapshot() or args.no_summary else "[law_change_auto] 우선 안내 대상이 없습니다. candidate_selection.json의 참고·확인·보류 후보를 확인하세요.")
    else:
        # 단일 날짜 모드
        target_date = _resolve_target_date(args.date)
        print(f"[law_change_auto] 기준일자: {target_date.isoformat()}")

        created = _process_single_date(
            target_date,
            output_dir,
            monitored_laws,
            law_filter,
            create_example_if_empty=False,
            no_web_check=args.no_web_check,
            make_summary=not args.no_summary,
            summary_use_llm=not args.no_summary_llm,
            selection_review=args.selection_review,
        )
        if created:
            for path in created:
                print(f"[law_change_auto] 생성: {path.name}")
            print(f"[law_change_auto] 안내서 초안 생성 ({len(created)}개); 배포 전 원문 대조·전 페이지 검수 필요")
        else:
            print("[law_change_auto] 우선 안내 대상이 없습니다. 후보 선별 검토 목록을 확인하세요.")


if __name__ == "__main__":
    main()
