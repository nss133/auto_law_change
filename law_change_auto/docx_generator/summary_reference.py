"""Summary-specific retained template and four-section content contract."""
from copy import deepcopy
import hashlib
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

from lxml import etree as E

NS={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
W='{'+NS['w']+'}'
SUMMARY_REFERENCE_SHA256='2e662890207f0c260cc55f0b8c0e8aa041dbdaf473210d3cd01dccf43d303922'
HEADINGS=['Ⅰ. 개요','Ⅱ. 당사 관련성 높은 주요 개정 사항','Ⅲ. 전체 법령 제·개정 요약','Ⅳ. 미결사항']


def summary_reference_path():
    p=Path(__file__).resolve().parents[2]/'templates/summary_reference.docx'
    if hashlib.sha256(p.read_bytes()).hexdigest()!=SUMMARY_REFERENCE_SHA256:
        raise ValueError('요약 참고 양식 해시 불일치')
    return p


def _replace_text(p,text):
    runs=p.findall('w:r',NS)
    r=deepcopy(next((r for r in runs if ''.join(r.xpath('.//w:t/text()',namespaces=NS)).strip()),runs[0])) if runs else E.Element(W+'r')
    for el in list(p):
        if el.tag!=W+'pPr':p.remove(el)
    for el in list(r):
        if el.tag!=W+'rPr':r.remove(el)
    t=E.SubElement(r,W+'t');t.text=text;t.set('{http://www.w3.org/XML/1998/namespace}space','preserve');p.append(r)
    return p


def render_summary(path,*,title,month,overview,relevant_rows,all_rows,issues):
    with ZipFile(summary_reference_path()) as z:
        parts={n:z.read(n) for n in z.namelist()};infos={n:z.getinfo(n) for n in z.namelist()}
    xml=E.fromstring(parts['word/document.xml']);b=xml.find('w:body',NS)
    ps=b.findall('w:p',NS);tables=b.findall('w:tbl',NS);sect=deepcopy(b.find('w:sectPr',NS))
    def p(index,text=None):
        value=deepcopy(ps[index])
        if text is not None:_replace_text(value,text)
        if index in (3,5,7,9):
            pr=value.find('w:pPr',NS)
            if pr is None:pr=E.SubElement(value,W+'pPr')
            E.SubElement(pr,W+'keepNext')
        return value
    def table(index,rows):
        t=deepcopy(tables[index]);pattern=deepcopy(t.findall('w:tr',NS)[1])
        for row in t.findall('w:tr',NS)[1:]:t.remove(row)
        head=t.find('w:tr',NS);pr=head.find('w:trPr',NS)
        if pr is None:pr=E.SubElement(head,W+'trPr')
        E.SubElement(pr,W+'tblHeader')
        for values in rows:
            row=deepcopy(pattern);pr=row.find('w:trPr',NS)
            if pr is None:pr=E.SubElement(row,W+'trPr')
            E.SubElement(pr,W+'cantSplit')
            for cell,value in zip(row.findall('w:tc',NS),values):
                _replace_text(cell.find('w:p',NS),str(value))
            t.append(row)
        return t
    content=[p(0,title),p(1,month),p(2),p(3),p(4,overview),p(5)]
    if relevant_rows:content.append(table(0,relevant_rows))
    else:content.append(p(4,'해당 기간 중 당사 관련성이 두드러지는 개정 사항은 없음.'))
    content += [p(6),p(7,f'Ⅲ. 전체 법령 제·개정 요약 ({len(all_rows)}건)'),table(1,all_rows),p(8),p(9)]
    content += [p(10,text) for text in issues]
    for el in list(b):b.remove(el)
    for el in content:b.append(el)
    b.append(sect)
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with ZipFile(path,'w',ZIP_DEFLATED) as z:
        for n,data in parts.items():
            z.writestr(infos[n],E.tostring(xml,encoding='UTF-8',xml_declaration=True,standalone=True) if n=='word/document.xml' else data)
    validate_summary(path,expected_rows=all_rows,expected_relevant=relevant_rows)
    return path


def validate_summary(path,*,expected_rows=None,expected_relevant=None):
    from docx import Document
    with ZipFile(summary_reference_path()) as a,ZipFile(path) as b:
        if b.testzip() is not None or set(a.namelist())!=set(b.namelist()):raise ValueError('요약 패키지 구성 불일치')
        for n in a.namelist():
            if n!='word/document.xml' and a.read(n)!=b.read(n):raise ValueError(f'요약 참고 서식 변경: {n}')
        ref=E.fromstring(a.read('word/document.xml'));out=E.fromstring(b.read('word/document.xml'))
        if E.tostring(ref.find('w:body/w:sectPr',NS))!=E.tostring(out.find('w:body/w:sectPr',NS)):
            raise ValueError('요약 페이지 설정 변경')
    doc=Document(path)
    from .reference import _signature
    ref_ps=ref.findall('w:body/w:p',NS)
    state=None;position=0
    for p in out.findall('w:body/w:p',NS):
        text=''.join(p.xpath('.//w:t/text()',namespaces=NS))
        if not text.strip():continue
        if position<3:index=position
        elif text.startswith('Ⅰ.'):index=3;state='overview'
        elif text.startswith('Ⅱ.'):index=5;state='relevant'
        elif text.startswith('Ⅲ.'):index=7;state='full'
        elif text.startswith('Ⅳ.'):index=9;state='issues'
        else:index=10 if state=='issues' else 4
        expected=ref_ps[index]
        if _signature(p.find('w:pPr',NS))!=_signature(expected.find('w:pPr',NS)):
            raise ValueError('요약 문단 역할별 스타일·간격 변경')
        er=next((r for r in expected.findall('w:r',NS) if ''.join(r.xpath('.//w:t/text()',namespaces=NS)).strip()),None)
        for r in p.findall('w:r',NS):
            if _signature(r.find('w:rPr',NS))!=_signature(er.find('w:rPr',NS) if er is not None else None):
                raise ValueError('요약 문단 역할별 글꼴·크기 변경')
        position+=1
    heads=[p.text for p in doc.paragraphs if p.text.startswith(('Ⅰ.','Ⅱ.','Ⅲ.','Ⅳ.'))]
    if len(heads)!=4 or any(not actual.startswith(expected) for actual,expected in zip(heads,HEADINGS)):
        raise ValueError('요약 4개 섹션 누락·순서 변경')
    blocks=list(out.find('w:body',NS));subset=[];full=[];section=None
    for el in blocks:
        text=''.join(el.xpath('.//w:t/text()',namespaces=NS))
        if el.tag==W+'p':
            if text.startswith('Ⅱ.'):section='subset'
            elif text.startswith('Ⅲ.'):section='full'
            elif text.startswith('Ⅳ.'):section='issues'
        elif el.tag==W+'tbl':
            template=ref.findall('w:body/w:tbl',NS)[0 if section=='subset' else 1]
            if _signature(el.find('w:tblPr',NS))!=_signature(template.find('w:tblPr',NS)) or _signature(el.find('w:tblGrid',NS))!=_signature(template.find('w:tblGrid',NS)):
                raise ValueError('요약 표 서식·열 너비 변경')
            rows=[[''.join(c.xpath('.//w:t/text()',namespaces=NS)) for c in r.findall('w:tc',NS)] for r in el.findall('w:tr',NS)[1:]]
            if section=='subset':subset.extend(rows)
            elif section=='full':full.extend(rows)
            else:raise ValueError('요약 표 위치 변경')
    if len(doc.tables)!=(2 if subset else 1):raise ValueError('요약 선별표/전체표 구성 불일치')
    if any(row[0]!=str(i) for i,row in enumerate(full,1)):raise ValueError('전체표 번호 누락·중복')
    mapping={r[0]:r for r in full}
    if any(mapping.get(r[0])!=r for r in subset):raise ValueError('선별표와 전체표 번호·내용 불일치')
    if expected_rows is not None and full!=[[str(x)for x in row]for row in expected_rows]:raise ValueError('전체 요약 원문 대응 불일치')
    if expected_relevant is not None and subset!=[[str(x)for x in row]for row in expected_relevant]:raise ValueError('회사 관련 항목 표 누락·불일치')
    return {'full_count':len(full),'relevant_count':len(subset),'sections':heads}
