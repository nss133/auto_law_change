from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

# comp_matching_auto.matcher.law_db_schema.DDL_LAW_ARTICLES 와 동일 스키마 유지
_COMP_MATCHING_DDL = """
CREATE TABLE IF NOT EXISTS law_articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    law_name TEXT NOT NULL,
    article_ref TEXT NOT NULL,
    text TEXT NOT NULL,
    mst TEXT,
    source TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_law_articles_law_name ON law_articles(law_name);
""".strip()


def _unwrap_law_root(payload: dict[str, Any]) -> dict[str, Any]:
    if "법령" in payload and isinstance(payload["법령"], dict):
        return payload["법령"]
    return payload


def _normalize_jo_list(jo_block: Any) -> list[dict[str, Any]]:
    if jo_block is None:
        return []
    if isinstance(jo_block, list):
        items = jo_block
    elif isinstance(jo_block, dict):
        units = jo_block.get("조문단위")
        if units is None:
            return []
        if isinstance(units, dict):
            items = [units]
        elif isinstance(units, list):
            items = units
        else:
            return []
    else:
        return []
    out: list[dict[str, Any]] = []
    for x in items:
        if isinstance(x, dict):
            out.append(x)
    return out


def _format_article_ref(jo_num: str, jo_branch: str) -> str:
    n = (jo_num or "").strip()
    b = (jo_branch or "").strip()
    if not n:
        return ""
    if b and b != "0":
        return f"제{n}조의{b}"
    return f"제{n}조"


_ws_re = re.compile(r"\s+")


def _clean_text(s: str) -> str:
    s = (s or "").replace("\u00a0", " ").strip()
    return _ws_re.sub(" ", s)


# lawService JSON: 본문이 조문내용이 아니라 항·호·목 트리에만 있는 경우가 많음
_STRUCT_KEYS = frozenset({"항", "호", "목", "항단위", "호단위", "목단위"})
_TEXT_KEYS = ("항내용", "호내용", "목내용")


def _walk_law_hang_ho_tree(node: Any, parts: list[str]) -> None:
    """항/호/목 중첩 구조에서 텍스트만 수집 (조문내용 제외)."""
    if node is None:
        return
    if isinstance(node, list):
        for el in node:
            _walk_law_hang_ho_tree(el, parts)
        return
    if not isinstance(node, dict):
        return
    for tk in _TEXT_KEYS:
        v = node.get(tk)
        if isinstance(v, str) and v.strip():
            parts.append(_clean_text(v))
    for sk in _STRUCT_KEYS:
        if sk in node:
            _walk_law_hang_ho_tree(node[sk], parts)


def _full_article_body_from_api_item(item: dict[str, Any]) -> str:
    """조문단위 dict에서 조문내용 + 항·호·목 트리 본문을 한 문자열로 합친다."""
    base = _clean_text(str(item.get("조문내용", "") or "").strip())
    nested_parts: list[str] = []
    for root_key in ("항", "호", "목"):
        if root_key in item:
            _walk_law_hang_ho_tree(item[root_key], nested_parts)
    nested = _clean_text(" ".join(nested_parts))
    if base and nested:
        return _clean_text(f"{base} {nested}")
    if base:
        return base
    return nested


def law_api_payload_to_comp_matching_rows(
    api_payload: dict[str, Any],
    *,
    law_name_override: str | None = None,
    only_jo: bool = True,
) -> list[dict[str, str]]:
    """법령 본문 API(JSON dict)를 comp_matching_auto 용 행 목록으로 변환.

    각 행: law_name, article_ref, text
    only_jo=True 이면 실질 조항만 남긴다 (국가법령 API는 조문여부가 '조문', 구스키마는 '조').
    '전문'·편·장·절·관 등은 제외.
    """
    root = _unwrap_law_root(api_payload)
    basic = root.get("기본정보")
    if not isinstance(basic, dict):
        basic = root
    law_name = law_name_override or basic.get("법령명_한글") or basic.get("법령명") or ""
    law_name = str(law_name).strip()

    jo_root = root.get("조문")
    if not isinstance(jo_root, dict):
        jo_root = {}

    rows: list[dict[str, str]] = []
    for item in _normalize_jo_list(jo_root):
        typ = str(item.get("조문여부") or "").strip()
        if typ == "Y":
            typ = "조문"
        is_article = typ in ("조", "조문")
        if only_jo and not is_article:
            continue

        num = str(item.get("조문번호", "")).strip()
        branch = str(
            item.get("조문가지번호", item.get("조가지번호", ""))
        ).strip()
        title = str(item.get("조문제목", "") or "").strip()
        content = _full_article_body_from_api_item(item)
        if not content and not num:
            continue

        article_ref = _format_article_ref(num, branch)
        if title and article_ref:
            head = f"{article_ref}({title})"
        elif article_ref:
            head = article_ref
        elif title:
            head = title
        else:
            head = ""

        # 매칭용 텍스트: 조문 참조 + 제목 + 본문 (본문에 이미 제N조가 있으면 중복될 수 있음)
        if head and content:
            text = f"{head} {content}".strip()
        else:
            text = content or head

        rows.append(
            {
                "law_name": law_name,
                "article_ref": article_ref or head,
                "text": text,
            }
        )
    return rows


def write_comp_matching_csv(rows: list[dict[str, str]], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        pd.DataFrame(columns=["law_name", "article_ref", "text"]).to_csv(
            path, index=False, encoding="utf-8-sig"
        )
        return path
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")
    return path


def write_comp_matching_csv_excel(rows: list[dict[str, str]], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        pd.DataFrame(columns=["law_name", "article_ref", "text"]).to_excel(path, index=False)
        return path
    pd.DataFrame(rows).to_excel(path, index=False)
    return path


def write_comp_matching_sqlite(
    rows: list[dict[str, str]],
    path: str | Path,
    *,
    source: str,
    table: str = "law_articles",
    law_name_to_mst: dict[str, str] | None = None,
) -> Path:
    """comp_matching_auto --law-db 와 동일한 SQLite 스키마로 저장. 기존 테이블은 비운 뒤 삽입."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    mst_map = law_name_to_mst or {}

    conn = sqlite3.connect(path)
    try:
        conn.executescript(_COMP_MATCHING_DDL)
        if table != "law_articles":
            raise ValueError("현재는 테이블 law_articles 만 지원합니다.")
        conn.execute("DELETE FROM law_articles")
        q = """
        INSERT INTO law_articles (law_name, article_ref, text, mst, source, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """
        batch = [
            (
                r["law_name"],
                r["article_ref"],
                r["text"],
                mst_map.get(r["law_name"]),
                source,
                now,
            )
            for r in rows
        ]
        conn.executemany(q, batch)
        conn.commit()
    finally:
        conn.close()
    return path


def merge_replace_law_articles(
    new_rows: list[dict[str, str]],
    path: str | Path,
    *,
    source: str,
    law_name_to_mst: dict[str, str] | None = None,
    table: str = "law_articles",
) -> Path:
    """기존 SQLite를 유지한 채, new_rows에 등장하는 law_name 행만 삭제 후 다시 삽입."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    mst_map = law_name_to_mst or {}
    names = {r["law_name"] for r in new_rows}
    if table != "law_articles":
        raise ValueError("현재는 테이블 law_articles 만 지원합니다.")

    conn = sqlite3.connect(path)
    try:
        conn.executescript(_COMP_MATCHING_DDL)
        if names:
            ph = ",".join("?" * len(names))
            conn.execute(f"DELETE FROM law_articles WHERE law_name IN ({ph})", tuple(names))
        q = """
        INSERT INTO law_articles (law_name, article_ref, text, mst, source, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """
        batch = [
            (
                r["law_name"],
                r["article_ref"],
                r["text"],
                mst_map.get(r["law_name"]),
                source,
                now,
            )
            for r in new_rows
        ]
        conn.executemany(q, batch)
        conn.commit()
    finally:
        conn.close()
    return path
