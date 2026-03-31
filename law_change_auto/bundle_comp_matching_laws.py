"""
law_change_auto 의 모니터링 법령 목록(기본: data/monitored_laws.xlsx)을
국가법령 OPEN API로 조회해 comp_matching_auto 용 SQLite(law_articles)로 보낸다.

각 목록 행마다 lsRlt(관련법령)로 법률·시행령·시행규칙 묶음을 확장한 뒤 본문을 수집한다.

저장소 루트에서:
  python -m law_change_auto.bundle_comp_matching_laws

기본 출력: 형제 폴더 comp_matching_auto/data/laws_monitored.sqlite
환경변수 LAW_GO_API_KEY 필수.

예:
  python -m law_change_auto.bundle_comp_matching_laws -o /path/to/laws.sqlite
  python -m law_change_auto.bundle_comp_matching_laws --legacy-single
  python -m law_change_auto.bundle_comp_matching_laws --only "개인정보보호법,남녀고용평등법,금융소비자보호에 관한 법률"
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_REPO_ROOT / ".env")

from law_change_auto.config.monitored_laws_loader import MonitoredLaw, load_monitored_laws
from law_change_auto.exporters.comp_matching_law_export import (
    law_api_payload_to_comp_matching_rows,
    merge_replace_law_articles,
    write_comp_matching_csv,
    write_comp_matching_sqlite,
)
from law_change_auto.fetchers.law_body_fetcher import (
    fetch_law_body_json,
    resolve_mst_and_id_from_law_name,
)
from law_change_auto.fetchers.law_related_fetcher import expand_law_family_law_ids


def _default_output_sqlite() -> Path:
    sibling = _REPO_ROOT.parent / "comp_matching_auto" / "data" / "laws_monitored.sqlite"
    return sibling


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="모니터링 법령 N건 API 수집(법률+시행령+시행규칙 확장) → comp_matching_auto SQLite",
    )
    p.add_argument(
        "-o",
        "--output",
        type=str,
        default="",
        help="출력 SQLite 경로 (미지정 시 형제 comp_matching_auto/data/laws_monitored.sqlite)",
    )
    p.add_argument(
        "--monitored-path",
        type=str,
        default="data/monitored_laws.xlsx",
        help="법령 목록 엑셀 (저장소 루트 기준, 법령명 열 필수)",
    )
    p.add_argument(
        "--sleep",
        type=float,
        default=0.35,
        help="각 법령ID 본문 조회 사이 대기(초)",
    )
    p.add_argument(
        "--include-structure",
        action="store_true",
        help="편·장·절·관 행 포함",
    )
    p.add_argument(
        "--also-csv",
        type=str,
        default="",
        help="같은 내용을 CSV로도 저장할 경로(선택)",
    )
    p.add_argument(
        "--legacy-single",
        action="store_true",
        help="확장 없이 lsStmd 첫 1건만 본문 수집(이전 동작)",
    )
    p.add_argument(
        "--only",
        type=str,
        default="",
        help="쉼표로 구분한 법령명만 수집(엑셀 전체 대신). 기본 출력 파일이 있으면 해당 law_name 행만 교체(병합).",
    )
    p.add_argument(
        "--replace-entire-db",
        action="store_true",
        help="--only 와 함께 쓰면 병합하지 않고 출력 SQLite 전체를 이번 수집분만으로 덮어씀(주의).",
    )
    return p.parse_args(argv)


def _fetch_body_retry(law_id: str, *, sleep_s: float, attempts: int = 4) -> dict[str, Any]:
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fetch_law_body_json(law_id=law_id)
        except Exception as e:
            last = e
            if i < attempts - 1:
                time.sleep(max(sleep_s, 2.0))
    assert last is not None
    raise last


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    out = Path(args.output) if args.output.strip() else _default_output_sqlite()
    mon_path = _REPO_ROOT / args.monitored_path

    only_names = [x.strip() for x in args.only.split(",") if x.strip()]
    if only_names:
        laws = [MonitoredLaw(name=n, note=None) for n in only_names]
        print(f"[bundle] --only 지정 법령 {len(laws)}건: {', '.join(only_names)}")
    else:
        try:
            laws = load_monitored_laws(mon_path)
        except FileNotFoundError as e:
            print(e, file=sys.stderr)
            return 2
        print(f"[bundle] 모니터링 법령 {len(laws)}건 (목록: {mon_path})")
    print(f"[bundle] 출력 SQLite: {out.resolve()}")
    if not args.legacy_single:
        print("[bundle] 법률·시행령·시행규칙 lsRlt 확장 모드", flush=True)

    only_jo = not args.include_structure
    all_rows: list[dict[str, str]] = []
    law_name_to_mst: dict[str, str] = {}
    seen_article_keys: set[tuple[str, str]] = set()

    for i, law in enumerate(laws):
        print(f"[{i + 1}/{len(laws)}] {law.name} …", flush=True)
        before_n = len(all_rows)

        if args.legacy_single:
            mst, law_id = resolve_mst_and_id_from_law_name(law.name)
            if not mst and not law_id:
                print("         [건너뜀] lsStmd 검색에서 MST/법령ID 없음", flush=True)
                time.sleep(args.sleep)
                continue
            try:
                payload = (
                    fetch_law_body_json(mst=mst)
                    if mst
                    else fetch_law_body_json(law_id=law_id)
                )
            except Exception as e:
                print(f"         [건너뜀] 본문 API: {e}", flush=True)
                time.sleep(args.sleep)
                continue
            rows = law_api_payload_to_comp_matching_rows(payload, only_jo=only_jo)
            for r in rows:
                key = (r["law_name"], r["article_ref"])
                if key in seen_article_keys:
                    continue
                seen_article_keys.add(key)
                all_rows.append(r)
            for nm in {r["law_name"] for r in rows}:
                if mst:
                    law_name_to_mst[nm] = mst
            print(
                f"         → 조문 {len(rows)}행 (legacy, MST={mst or '-'})",
                flush=True,
            )
            time.sleep(args.sleep)
            continue

        try:
            family = expand_law_family_law_ids(law.name)
        except Exception as e:
            print(f"         [건너뜀] 관련법령 확장: {e}", flush=True)
            time.sleep(args.sleep)
            continue

        if not family:
            print("         [건너뜀] 확장된 법령 ID 없음", flush=True)
            time.sleep(args.sleep)
            continue

        for lid, lname in family:
            try:
                payload = _fetch_body_retry(lid, sleep_s=args.sleep)
            except Exception as e:
                print(f"         [건너뜀] {lname} 본문: {e}", flush=True)
                time.sleep(args.sleep)
                continue
            rows = law_api_payload_to_comp_matching_rows(payload, only_jo=only_jo)
            for r in rows:
                key = (r["law_name"], r["article_ref"])
                if key in seen_article_keys:
                    continue
                seen_article_keys.add(key)
                all_rows.append(r)
            for nm in {r["law_name"] for r in rows}:
                law_name_to_mst[nm] = lid
            time.sleep(args.sleep)

        added = len(all_rows) - before_n
        names = " / ".join(f"{n}({lid})" for lid, n in family)
        print(f"         → 확장 {len(family)}법령, 신규 조문 {added}행", flush=True)
        print(f"            ({names})", flush=True)

    if not all_rows:
        print("[bundle] 수집된 조문이 없습니다. API 키·IP·목록 파일을 확인하세요.", file=sys.stderr)
        return 3

    if only_names and not args.replace_entire_db:
        source = f"law_change_auto:bundle+family:only:{len(only_names)}건"
        merge_replace_law_articles(
            all_rows,
            out,
            source=source,
            law_name_to_mst=law_name_to_mst,
        )
        print(
            f"[bundle] SQLite 병합(해당 law_name 교체): {out.resolve()} "
            f"(이번 수집 {len(all_rows)}행, 고유 조문키 {len(seen_article_keys)})",
            flush=True,
        )
    else:
        source = (
            f"law_change_auto:bundle+family:only-full:{len(only_names)}건"
            if only_names
            else f"law_change_auto:bundle+family:{mon_path.name}"
        )
        write_comp_matching_sqlite(
            all_rows,
            out,
            source=source,
            law_name_to_mst=law_name_to_mst,
        )
        print(
            f"[bundle] SQLite 완료(전체 덮어쓰기): {out.resolve()} "
            f"(총 {len(all_rows)}행, 고유 조문키 {len(seen_article_keys)})",
            flush=True,
        )

    if args.also_csv.strip():
        csv_path = Path(args.also_csv.strip())
        write_comp_matching_csv(all_rows, csv_path)
        print(f"[bundle] CSV 백업: {csv_path.resolve()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
