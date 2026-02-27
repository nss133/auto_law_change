from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd


@dataclass
class MonitoredLaw:
    name: str
    note: str | None = None


def load_monitored_laws(path: Path | str = "data/monitored_laws.xlsx") -> list[MonitoredLaw]:
    """모니터링 대상 법령 리스트를 엑셀에서 읽어온다.

    예상 시트 구조:
      - 법령명 (필수)
      - 비고 (선택)
    """
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(
            f"모니터링 대상 법령 리스트 파일을 찾을 수 없습니다: {file_path}\n"
            "예상 위치에 '법령명', '비고' 컬럼을 가진 monitored_laws.xlsx를 생성해주세요."
        )

    df = pd.read_excel(file_path)

    if "법령명" not in df.columns:
        raise ValueError("엑셀 파일에 '법령명' 컬럼이 필요합니다.")

    laws: Iterable[MonitoredLaw] = (
        MonitoredLaw(name=str(row["법령명"]).strip(), note=str(row.get("비고", "")).strip() or None)
        for _, row in df.iterrows()
        if str(row["법령명"]).strip()
    )
    return list(laws)

