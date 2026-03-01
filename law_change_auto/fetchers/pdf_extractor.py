"""PDF 텍스트 추출 및 이미지 변환 유틸리티."""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Optional, Union

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (law_change_auto)",
    "Referer": "https://www.fsc.go.kr/",
}


def extract_text_from_pdf_bytes(data: bytes) -> str:
    """PDF 바이트에서 텍스트를 추출한다. PyMuPDF 사용."""
    try:
        import fitz  # PyMuPDF

        # MuPDF가 일부 PDF에서 stderr로 "syntax error: invalid key in dict" 출력 방지
        fitz.TOOLS.mupdf_display_errors(False)

        doc = fitz.open(stream=data, filetype="pdf")
        parts: list[str] = []
        for page in doc:
            try:
                text = page.get_text()
                if text and text.strip():
                    parts.append(text.strip())
            except Exception:
                pass
        doc.close()
        result = "\n".join(parts)
        # 연속 빈 줄 정리
        result = re.sub(r"\n{3,}", "\n\n", result)
        return result.strip()
    except Exception:
        return ""


def extract_text_from_pdf_file(path: Path | str) -> str:
    """로컬 PDF 파일에서 텍스트를 추출한다."""
    path = Path(path)
    if not path.exists():
        return ""
    data = path.read_bytes()
    return extract_text_from_pdf_bytes(data)


def download_pdf(
    url: str,
    timeout: int = 30,
    session: Optional["requests.Session"] = None,
) -> Optional[bytes]:
    """PDF URL에서 바이트를 다운로드한다. session 제공 시 세션 쿠키로 요청."""
    try:
        s = session or requests
        resp = s.get(url, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        ct = resp.headers.get("Content-Type", "").lower()
        content = resp.content
        is_pdf = "pdf" in ct or content[:4] == b"%PDF"
        if not is_pdf:
            return None
        return content
    except Exception:
        return None


def fetch_pdf_text(
    url: str,
    session: Optional["requests.Session"] = None,
) -> str:
    """PDF URL에서 다운로드 후 텍스트 추출. session 제공 시 세션 쿠키로 요청."""
    data = download_pdf(url, session=session)
    if not data:
        return ""
    return extract_text_from_pdf_bytes(data)


def _find_first_page_with_text(doc, search_text: str) -> int:
    """텍스트가 포함된 첫 페이지 인덱스. 공백 제거 후 비교. 없으면 0."""
    normalized = re.sub(r"\s+", "", search_text)
    for i, page in enumerate(doc):
        try:
            text = page.get_text() or ""
            if normalized in re.sub(r"\s+", "", text):
                return i
        except Exception:
            pass
    return 0


def render_pdf_pages_to_images(
    pdf_source: Union[bytes, Path, str],
    dpi: int = 150,
    from_page_with_text: Optional[str] = None,
) -> list[io.BytesIO]:
    """
    PDF의 각 페이지를 PNG 이미지로 렌더링하여 BytesIO 스트림 목록으로 반환.
    from_page_with_text 지정 시 해당 텍스트가 나오는 페이지부터 끝까지만 포함.
    add_picture() 등에 바로 전달 가능.
    """
    result: list[io.BytesIO] = []
    try:
        import fitz  # PyMuPDF

        fitz.TOOLS.mupdf_display_errors(False)

        path = Path(pdf_source) if isinstance(pdf_source, (Path, str)) else None
        if path and path.exists():
            doc = fitz.open(path)
        elif isinstance(pdf_source, bytes):
            doc = fitz.open(stream=pdf_source, filetype="pdf")
        else:
            return result

        start_idx = 0
        if from_page_with_text:
            start_idx = _find_first_page_with_text(doc, from_page_with_text)

        zoom = dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)

        try:
            for i in range(start_idx, len(doc)):
                page = doc[i]
                try:
                    pix = page.get_pixmap(matrix=matrix, alpha=False)
                    png_bytes = pix.tobytes("png")
                    result.append(io.BytesIO(png_bytes))
                except Exception:
                    pass
        finally:
            doc.close()

    except Exception:
        pass
    return result
