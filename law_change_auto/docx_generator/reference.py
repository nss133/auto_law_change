"""Retained user DOCX is the design authority; change only document.xml."""
from copy import deepcopy
import hashlib
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

from lxml import etree as E

NS = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
W = '{' + NS['w'] + '}'
REFERENCE_SHA256 = 'f182e71716ef43e55b107acce72dcb056583a825d7ec27ed532f7982332f72de'


def reference_path():
    path = Path(__file__).resolve().parents[2] / 'templates/guide_reference.docx'
    if hashlib.sha256(path.read_bytes()).hexdigest() != REFERENCE_SHA256:
        raise ValueError('지정 참고 양식 해시 불일치. 새 양식을 임의로 승인할 수 없음.')
    return path


class ReferenceDocument:
    def __init__(self):
        with ZipFile(reference_path()) as z:
            self.parts = {n: z.read(n) for n in z.namelist()}
            self.infos = {n: z.getinfo(n) for n in z.namelist()}
        self.xml = E.fromstring(self.parts['word/document.xml'])
        b = self.xml.find('w:body', NS)
        self.paras = b.findall('w:p', NS)
        self.table = b.find('w:tbl', NS)
        self.sect = deepcopy(b.find('w:sectPr', NS))
        self.body = []

    def para(self, index, text=None):
        p = deepcopy(self.paras[index])
        if text is not None:
            runs = p.findall('w:r', NS)
            r = deepcopy(runs[0]) if runs else E.Element(W+'r')
            for el in list(p):
                if el.tag != W+'pPr':
                    p.remove(el)
            for el in list(r):
                if el.tag != W+'rPr':
                    r.remove(el)
            t = E.SubElement(r, W+'t'); t.text = text
            t.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
            p.append(r)
        if index in (5, 10, 17, 20):
            pr = p.find('w:pPr', NS)
            if pr is None:
                pr = E.SubElement(p, W+'pPr')
            if pr.find('w:keepNext', NS) is None:
                E.SubElement(pr, W+'keepNext')
        return p

    def comparison(self, rows):
        table = deepcopy(self.table)
        for row in table.findall('w:tr', NS)[1:]:
            table.remove(row)
        pattern = self.table.findall('w:tr', NS)[1]
        for data in rows:
            row = deepcopy(pattern)
            for side, cell in zip(('old', 'new'), row.findall('w:tc', NS)):
                p = cell.find('w:p', NS)
                for el in list(p):
                    if el.tag != W+'pPr':
                        p.remove(el)
                segments = getattr(data, side+'_segments') or [(getattr(data, side+'_text') or '', 'normal')]
                for text, style in segments:
                    for i, line in enumerate(text.split('\n')):
                        r = E.SubElement(p, W+'r'); pr = E.SubElement(r, W+'rPr')
                        E.SubElement(pr, W+'sz').set(W+'val', '18')
                        if style in ('ins', 'del'):
                            E.SubElement(pr, W+'color').set(W+'val', 'FF0000')
                        if style == 'del':
                            E.SubElement(pr, W+'u').set(W+'val', 'single')
                        if i:
                            E.SubElement(r, W+'br')
                        t = E.SubElement(r, W+'t'); t.text = line
                        t.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
            table.append(row)
        return table

    def save(self, path):
        b = self.xml.find('w:body', NS)
        for el in list(b):
            b.remove(el)
        for el in self.body:
            b.append(el)
        b.append(self.sect)
        xml = E.tostring(self.xml, encoding='UTF-8', xml_declaration=True, standalone=True)
        path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
        with ZipFile(path, 'w', ZIP_DEFLATED) as z:
            for n, raw in self.parts.items():
                z.writestr(self.infos[n], xml if n == 'word/document.xml' else raw)
        validate_reference(path)
        return path


def validate_reference(path):
    with ZipFile(reference_path()) as ref, ZipFile(path) as doc:
        if doc.testzip() is not None or set(ref.namelist()) != set(doc.namelist()):
            raise ValueError('DOCX 패키지 구성 불일치')
        for n in ref.namelist():
            if n != 'word/document.xml' and ref.read(n) != doc.read(n):
                raise ValueError(f'참고 양식 파트 불일치: {n}')
        a = E.fromstring(ref.read('word/document.xml')).find('w:body/w:sectPr', NS)
        b = E.fromstring(doc.read('word/document.xml')).find('w:body/w:sectPr', NS)
        if E.tostring(a) != E.tostring(b):
            raise ValueError('참고 양식 페이지 설정 불일치')
        reference = E.fromstring(ref.read('word/document.xml'))
        actual = E.fromstring(doc.read('word/document.xml'))
        _validate_roles(reference, actual)


def _signature(element):
    if element is None:
        return None
    return (element.tag, tuple(sorted(element.attrib.items())),
            tuple(_signature(c) for c in element if c.tag != W+'keepNext'))


def _validate_roles(reference, actual):
    """Package equality alone cannot detect direct font/spacing/section mutations."""
    import re
    ref = reference.findall('w:body/w:p', NS)
    ps = actual.findall('w:body/w:p', NS)
    def text(p):
        return ''.join(p.xpath('.//w:t/text()', namespaces=NS))
    def role(p, index):
        expected = ref[index]
        if _signature(p.find('w:pPr', NS)) != _signature(expected.find('w:pPr', NS)):
            raise ValueError(f'문단 역할별 간격·정렬·스타일 불일치: {text(p)[:35]}')
        runs = p.findall('w:r', NS)
        er = expected.find('w:r/w:rPr', NS)
        if any(_signature(r.find('w:rPr', NS)) != _signature(er) for r in runs):
            raise ValueError(f'문단 역할별 글꼴·크기·강조 불일치: {text(p)[:35]}')
    filled = [p for p in ps if text(p).strip()]
    if not filled:
        raise ValueError('안내서 내용 없음')
    if text(filled[0]) == '법령제·개정 안내서 목차':
        role(filled[0], 0); role(filled[1], 1)
        for p in filled[2:]:
            role(p, 6)
        return
    title_sig = _signature(ref[0].find('w:pPr', NS))
    groups = []
    for p in filled:
        if _signature(p.find('w:pPr', NS)) == title_sig:
            groups.append([])
        if not groups:
            raise ValueError('참고 양식 표제 누락')
        groups[-1].append(p)
    for group in groups:
        if len(group) < 11:
            raise ValueError('필수 문단 누락')
        for i in range(4):
            role(group[i], i)
        if not re.fullmatch(r'\d{4}\. \d{2}\.', text(group[2])) or text(group[3]) != '법 무 팀':
            raise ValueError('발행월·부서 표시 불일치')
        heads = [text(p) for p in group[4:] if re.fullmatch(r'\d\. (개정이유|주요내용|의견제출기한|파급효과|신구조문 대비표)', text(p))]
        names = ['개정이유', '주요내용']
        if any('의견제출기한' in h for h in heads):
            names.append('의견제출기한')
        names += ['파급효과', '신구조문 대비표']
        if heads != [f'{i}. {name}' for i, name in enumerate(names, 1)]:
            raise ValueError('참고 양식 필수 섹션·순서 불일치')
        current = None
        for p in group[4:]:
            value = text(p)
            if value in heads:
                current = value.split('. ', 1)[1]
                role(p, {'개정이유': 5, '주요내용': 10, '의견제출기한': 5,
                         '파급효과': 17, '신구조문 대비표': 20}[current])
            else:
                if current is None:
                    raise ValueError('필수 개정이유 제목 누락·임의 섹션 추가')
                role(p, {'개정이유': 6, '주요내용': 12, '의견제출기한': 6,
                         '파급효과': 18, '신구조문 대비표': 6}[current])
    for table in actual.findall('w:body/w:tbl', NS):
        if _signature(table.find('w:tblPr', NS)) != _signature(reference.find('w:body/w:tbl/w:tblPr', NS)):
            raise ValueError('신구조문 표 서식 불일치')
        for row in table.findall('w:tr', NS)[1:]:
            for cell in row.findall('w:tc', NS):
                for p in cell.findall('w:p', NS):
                    for r in p.findall('w:r', NS):
                        size = r.find('w:rPr/w:sz', NS)
                        if size is None or size.get(W+'val') != '18':
                            raise ValueError('신구조문 표 글자 크기 불일치')


def _fmt(d):
    return f'{d.year}. {d.month}. {d.day}.' if d else ''


def render_guides(details, target_date, path):
    from .generator import guide_display_title, _clean_revision_paras, _promulgation_kind_for_meta
    from ..services.enrichment import impact_for_document, source_limit
    doc = ReferenceDocument()
    for index, d in enumerate(details):
        if index:
            p = doc.para(4)
            r = E.SubElement(p, W+'r'); E.SubElement(r, W+'br').set(W+'type', 'page')
            doc.body.append(p)
        m = d.meta
        title = guide_display_title(m)
        if d.publication_status == '시행':
            from dataclasses import replace
            title = guide_display_title(replace(m, change_type='시행'))
        if m.category == '입법예고':
            metadata = f'[예고일 {_fmt(m.announcement_date)}]'
        else:
            num = str(m.promulgation_no or m.law_number or '').lstrip('0')
            number = f' 제{num}호' if num and num != '9999' else ''
            metadata = f'[시행 {_fmt(m.effective_date)}] [{_promulgation_kind_for_meta(m)}{number}, {m.amendment_date_str or _fmt(m.announcement_date)}'
            metadata += (', '+m.amendment_type if m.amendment_type else '')+']'
        reasons = _clean_revision_paras(d.reason_sections or d.combined_reason_and_main_sections)
        main = _clean_revision_paras(d.main_change_sections or d.combined_reason_and_main_sections)
        limit = source_limit(d)
        if limit:
            reasons = [f'※ {limit}']
            main = ['해당 개정문·대비표와 부칙을 확인한 후 보완이 필요함.']
        reasons = reasons or ['※ 개정이유 원문 확인이 필요함.']
        main = main or ['※ 주요내용 원문 확인이 필요함.']
        doc.body += [doc.para(0, title), doc.para(1, metadata), doc.para(2, target_date.strftime('%Y. %m.')),
                     doc.para(3), doc.para(4), doc.para(5, '1. 개정이유')]
        doc.body += [doc.para(6, t) for t in reasons] + [doc.para(9), doc.para(10, '2. 주요내용'), doc.para(11)]
        doc.body += [doc.para(12, t) for t in main] + [doc.para(15), doc.para(16)]
        n = 3
        if m.category == '입법예고' and d.opinion_deadline:
            doc.body += [doc.para(5, '3. 의견제출기한'), doc.para(6, d.opinion_deadline), doc.para(9)]
            n += 1
        doc.body += [doc.para(17, f'{n}. 파급효과'), doc.para(18, impact_for_document(d)), doc.para(19),
                     doc.para(20, f'{n+1}. 신구조문 대비표')]
        if d.article_comparisons:
            doc.body.append(doc.comparison(d.article_comparisons))
        elif d.comparison_pdf_paths:
            doc.body += [doc.para(6, f'※ 첨부 원문: {Path(p).name}') for _, p in d.comparison_pdf_paths]
        else:
            doc.body.append(doc.para(6, '※ 신구조문 대비표를 확인할 수 없음. 원문 확인이 필요함.'))
        doc.body.append(doc.para(22))
    return doc.save(path)


def render_toc(path, period, lines):
    doc = ReferenceDocument()
    doc.body = [doc.para(0, '법령제·개정 안내서 목차'), doc.para(1, period), doc.para(4)]
    doc.body += [doc.para(6, text) for text in lines]
    return doc.save(path)
