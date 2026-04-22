"""kordoc CLI를 이용한 신·구조문 대비표 추출 모듈.

법령 개정고시안 PDF/HWP에 내장된 신구조문대비표를 추출하여
ArticleComparisonRow 리스트로 반환한다.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from ..models import ArticleComparisonRow
from .gemini_client import fetch_comparison_json

# korean-law-marketplace에 내장된 kordoc CLI 경로
_KORDOC_CLI = Path(
    "/Users/nsss/.claude/plugins/marketplaces/korean-law-marketplace"
    "/node_modules/kordoc/dist/cli.js"
)

# GFM 구분선 행 (| --- | --- | 형식)
_GFM_SEP_RE = re.compile(r"^\|[\s\-:|]+\|")


def _run_kordoc(file_path: str) -> str | None:
    """kordoc CLI로 문서를 마크다운으로 변환. 실패 시 None 반환."""
    if not _KORDOC_CLI.exists():
        print(f"[kordoc] CLI 없음: {_KORDOC_CLI}")
        return None
    try:
        result = subprocess.run(
            ["node", str(_KORDOC_CLI), "--silent", "--no-header-footer", file_path],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return result.stdout
    except Exception as e:
        print(f"[kordoc] 실행 오류 ({file_path}): {e}")
        return None


# ──────────────────────────────────────────────
# GFM 파이프 테이블 파서
# ──────────────────────────────────────────────

def _parse_gfm_row(line: str) -> list[str]:
    """GFM 테이블 행 `| a | b | c |` → 셀 텍스트 리스트."""
    # 바깥 | 제거 후 | 로 분리
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [c.strip() for c in stripped.split("|")]


def _extract_gfm_tables(markdown: str) -> list[tuple[str, list[list[str]]]]:
    """마크다운에서 GFM 파이프 테이블을 추출.

    Returns: [(직전 맥락 300자, [[셀, ...], ...]), ...]
    """
    lines = markdown.splitlines()
    results: list[tuple[str, list[list[str]]]] = []

    i = 0
    while i < len(lines):
        line = lines[i]
        # 파이프가 있는 행으로 테이블 시작 탐지
        if "|" not in line:
            i += 1
            continue

        # 구분선 행(--- 등)은 건너뜀 — 다음 행부터 본격 파싱
        if _GFM_SEP_RE.match(line.strip()):
            i += 1
            continue

        # 연속된 파이프 행 수집
        table_start = i
        table_lines: list[str] = []
        while i < len(lines) and ("|" in lines[i] or _GFM_SEP_RE.match(lines[i].strip())):
            table_lines.append(lines[i])
            i += 1

        if len(table_lines) < 2:
            continue

        # 구분선 인덱스 찾기
        sep_idx = next(
            (j for j, tl in enumerate(table_lines) if _GFM_SEP_RE.match(tl.strip())),
            None,
        )
        if sep_idx is None:
            continue  # 구분선 없으면 GFM 테이블 아님

        # 헤더 + 데이터 행 파싱 (구분선 제외)
        rows: list[list[str]] = []
        for j, tl in enumerate(table_lines):
            if j == sep_idx:
                continue
            if not tl.strip() or not tl.strip().startswith("|"):
                continue
            rows.append(_parse_gfm_row(tl))

        if not rows:
            continue

        # 직전 맥락 수집
        context_start = max(0, table_start - 15)
        context = "\n".join(lines[context_start:table_start])

        results.append((context, rows))

    return results


# ──────────────────────────────────────────────
# 신구조문대비표 판별 & 변환
# ──────────────────────────────────────────────

def _is_comparison_table(context: str, rows: list[list[str]]) -> bool:
    """신구조문대비표 여부 판별."""
    # 1. 직전 맥락에 "신구조문" 키워드
    if re.search(r"신\s*[ㆍ·]?\s*구\s*조\s*문", context):
        return True
    # 2. 헤더 행에 "현행" 또는 "개정안" 포함
    if rows:
        header_text = " ".join(rows[0])
        if "현행" in header_text or "개정안" in header_text:
            return True
    # 3. 셀 본문에 "(현행과 같음)" 포함
    all_text = " ".join(c for row in rows for c in row)
    if "현행과 같음" in all_text:
        return True
    return False


def _rows_to_comparison(rows: list[list[str]]) -> list[ArticleComparisonRow]:
    """테이블 행 → ArticleComparisonRow 리스트.

    kordoc CLI GFM 출력 기준 두 가지 레이아웃:
    - 3열: col[0]+col[1] = 현행, col[2] = 개정안 (FSC 규정 고시안 PDF 전형)
    - 2열: col[0] = 현행, col[1] = 개정안
    """
    if not rows:
        return []

    max_cols = max((len(r) for r in rows), default=0)
    is_three_col = max_cols >= 3

    result: list[ArticleComparisonRow] = []
    for row in rows[1:]:  # 헤더 행 스킵
        if not row or all(not c for c in row):
            continue

        if is_three_col:
            old_text = " ".join(filter(None, row[:2])).strip()
            new_text = row[2].strip() if len(row) > 2 else ""
        else:
            old_text = row[0].strip() if row else ""
            new_text = row[1].strip() if len(row) > 1 else ""

        if not old_text and not new_text:
            continue
        # 헤더성 잔류 행 제거
        if re.fullmatch(r"현\s*행", old_text) and not new_text:
            continue

        article_m = re.search(r"(제\s*\d+\s*조(?:의\s*\d+)?)", old_text or new_text)
        article_no = article_m.group(1) if article_m else None

        title_m = re.search(r"\(([^)]{2,30})\)", old_text or new_text)
        article_title = title_m.group(1) if title_m else None

        result.append(
            ArticleComparisonRow(
                article_no=article_no,
                article_title=article_title,
                old_text=old_text,
                new_text=new_text,
            )
        )
    return result


# ──────────────────────────────────────────────
# 공개 API
# ──────────────────────────────────────────────

def _json_to_rows(items: list[dict]) -> list[ArticleComparisonRow]:
    """LLM JSON 응답 → ArticleComparisonRow 리스트."""
    rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        old_text = (item.get("old_text") or "").strip()
        new_text = (item.get("new_text") or "").strip()
        if not old_text and not new_text:
            continue
        rows.append(ArticleComparisonRow(
            article_no=item.get("article_no") or None,
            article_title=item.get("article_title") or None,
            old_text=old_text or None,
            new_text=new_text or None,
        ))
    return rows


def extract_comparison_from_file(
    file_path: str,
    *,
    law_name: str = "",
    llm_fallback: bool = False,
) -> list[ArticleComparisonRow]:
    """PDF/HWP/HWPX 단일 파일에서 신·구조문 대비표를 추출한다.

    llm_fallback=True 시 GFM 추출 실패 시 LLM(Groq→Gemini)으로 재시도.
    """
    markdown = _run_kordoc(file_path)
    if not markdown:
        return []

    all_rows: list[ArticleComparisonRow] = []
    for context, rows in _extract_gfm_tables(markdown):
        if not _is_comparison_table(context, rows):
            continue
        all_rows.extend(_rows_to_comparison(rows))

    # LLM fallback: GFM 결과가 없거나 3행 미만(부실 추출)이면 LLM도 시도
    if llm_fallback and re.search(r"신\s*[ㆍ·]?\s*구\s*조\s*문|개정\s*문|제\s*\d+\s*조", markdown):
        if len(all_rows) < 3:
            name = law_name or file_path.rsplit("/", 1)[-1]
            reason = "GFM 추출 실패" if not all_rows else f"GFM {len(all_rows)}행(부실) → LLM 보완"
            print(f"[kordoc] {name}: {reason} → LLM fallback")
            items = fetch_comparison_json(name, markdown)
            if items:
                llm_rows = _json_to_rows(items)
                if len(llm_rows) > len(all_rows):
                    all_rows = llm_rows

    return all_rows


def extract_comparison_via_llm(
    text: str,
    law_name: str,
) -> list[ArticleComparisonRow]:
    """텍스트(개정문·PDF plain text 등)를 LLM으로 신구조문 대비표로 변환.

    kordoc 파싱 없이 직접 텍스트를 받아 처리 — 개정문 HWP 파싱 결과에 활용.
    """
    if not text or not text.strip():
        return []
    items = fetch_comparison_json(law_name, text)
    if not items:
        return []
    return _json_to_rows(items)


def extract_comparison_from_pdf_paths(
    comparison_pdfs: list[tuple[str, str]],
) -> list[ArticleComparisonRow]:
    """comparison_pdf_paths [(label, saved_path), ...] 전체에서 신구대비표를 추출한다.

    pipeline.py의 comparison_pdf_paths 필드와 직접 연동.
    """
    all_rows: list[ArticleComparisonRow] = []
    for label, saved_path in comparison_pdfs:
        try:
            rows = extract_comparison_from_file(saved_path, law_name=label, llm_fallback=True)
            if rows:
                print(f"[kordoc] {label}: {len(rows)}행 추출")
                all_rows.extend(rows)
            else:
                print(f"[kordoc] {label}: 신구대비표 없음 또는 추출 실패")
        except Exception as e:
            print(f"[kordoc] {label} 추출 오류: {e}")
    return all_rows
