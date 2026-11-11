"""Fail-closed delivery gate: draft generation is never a completion claim."""
from dataclasses import asdict
import json
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

from .enrichment import digest, source_payload, source_limit, impact_for_document, valid_evidence
from ..docx_generator.reference import REFERENCE_SHA256, validate_reference

CHECKS = ('obligation_subject', 'scope_and_exceptions', 'effective_or_notice_status', 'source_correspondence')


def file_hash(path):
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def local_file(base, name):
    p = (base / name).resolve()
    if not p.is_relative_to(base.resolve()):
        raise ValueError('산출물 폴더 밖 경로는 검증·배포할 수 없음')
    return p


def write_manifest(base, details, created, file_map, report):
    base = Path(base)
    snapshot = base/'published_details.json'
    snapshot.write_text(json.dumps([asdict(d) for d in details], ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    guide_names = {p.name for p in file_map.values()}
    files = []
    for p in created:
        files.append({'name': p.name, 'sha256': file_hash(p) if p.is_file() else None,
                      'kind': 'guide' if p.name in guide_names else 'toc' if p.name == '목차.docx' else 'summary'})
    # Missing referenced attachments must block release, not disappear from the manifest.
    for d in details:
        for _, name in d.comparison_pdf_paths:
            p = base/Path(name).name
            if p.name not in {f['name'] for f in files}:
                files.append({'name': p.name, 'sha256': file_hash(p) if p.is_file() else None, 'kind': 'attachment'})
    manifest = {'schema': 1, 'reference_sha256': REFERENCE_SHA256, 'delivery_status': 'pending_review',
                'publication_policy': report.get('publication_policy', 'individual-and-digest'),
                'snapshot': snapshot.name, 'snapshot_sha256': file_hash(snapshot),
                'as_of': report.get('as_of') or report.get('guide_date'), 'period': report['period'],
                'collection_warnings': report.get('warnings', []), 'files': files,
                'details': [{'law_name': d.meta.law_name,
                             'output_file': file_map[id(d)].name if id(d) in file_map else None,
                             'source_sha256': digest(source_payload(d)),
                             'source_limit': source_limit(d), 'impact': impact_for_document(d),
                             'impact_sha256': digest(impact_for_document(d)),
                             'impact_evidence': d.impact_evidence,
                             'impact_correction': d.impact_correction,
                             'comparison_rows': len(d.article_comparisons)} for d in details]}
    if any(f['kind'] == 'summary' for f in files):
        from ..docx_generator.summary_reference import SUMMARY_REFERENCE_SHA256
        manifest['summary_reference_sha256'] = SUMMARY_REFERENCE_SHA256
    if report.get('selection_policy'):
        manifest.update(schema=2, selection_policy=report['selection_policy'],
                        selection_files={name: file_hash(base/name) for name in
                                         ('collected_details.json', 'candidate_details.json', 'candidate_selection.json', '후보선별_검토목록.md')})
    (base/'delivery_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    report.update(delivery_status='pending_review', delivery_manifest='delivery_manifest.json',
                  llm_successful_responses=sum(bool(d.impact_evidence.get('response_id')) and not d.impact_evidence.get('cache_hit') and not d.impact_evidence.get('reused_response') for d in details),
                  llm_cached_responses=sum(bool(d.impact_evidence.get('response_id')) and bool(d.impact_evidence.get('cache_hit')) and not d.impact_evidence.get('reused_response') for d in details),
                  llm_reused_responses=sum(bool(d.impact_evidence.get('response_id')) and bool(d.impact_evidence.get('reused_response')) for d in details),
                  source_limit_count=sum(bool(source_limit(d)) for d in details))
    return manifest


def review_template(base):
    base = Path(base)
    m = json.loads((base/'delivery_manifest.json').read_text())
    review = {'manifest_sha256': file_hash(base/'delivery_manifest.json'),
            'collection_review': {'status': 'pending', 'reviewer': '', 'notes': ''},
            'files': {f['name']: {'sha256': f['sha256'],
                         'semantic': {'status': 'pending', 'reviewer': '', 'notes': '',
                                      'checks': {k: False for k in CHECKS},
                                      'source_limits_acknowledged': False},
                         'visual': {'status': 'pending', 'reviewer': '', 'rendered_from_sha256': '',
                                    'pdf': '', 'pdf_sha256': '', 'pages_reviewed': 0, 'images': []}}
                      for f in m['files'] if f['kind'] != 'attachment'}}
    if m.get('schema') == 2:
        review['selection_review'] = {'status': 'pending', 'reviewer': '', 'notes': ''}
    return review


def validate_selection_registry(base, manifest, published):
    """Every detected candidate has a verifiable decision; only priority items may publish."""
    from .selection import (SELECTION_POLICY, LABELS, rules_contract, detail_from_dict,
                            validate_assessment, candidate_id)
    if manifest.get('selection_policy') != SELECTION_POLICY:
        raise ValueError('후보 선별 정책 불일치')
    names = {'collected_details.json', 'candidate_details.json', 'candidate_selection.json', '후보선별_검토목록.md'}
    if set(manifest.get('selection_files', {})) != names:
        raise ValueError('전체 후보·선별 근거 기록 누락')
    for name, expected in manifest['selection_files'].items():
        if file_hash(local_file(base, name)) != expected:
            raise ValueError(f'선별 기록 해시 불일치: {name}')
    collected = [detail_from_dict(d) for d in json.loads((base/'collected_details.json').read_text())]
    candidates = [detail_from_dict(d) for d in json.loads((base/'candidate_details.json').read_text())]
    ledger = json.loads((base/'candidate_selection.json').read_text())
    if ledger.get('schema') != 1 or ledger.get('policy') != SELECTION_POLICY or ledger.get('rules_sha256') != rules_contract()[1]:
        raise ValueError('선별 규칙 버전·해시 변경: 재선별과 검토 필요')
    if len(collected) != len(candidates) or len(candidates) != len(ledger['candidates']):
        raise ValueError('탐지 후보 또는 선별 기록 누락')
    if len({candidate_id(d) for d in candidates}) != len(candidates):
        raise ValueError('후보 개정 버전 중복')
    selected = []
    for i, (original, d, entry) in enumerate(zip(collected, candidates, ledger['candidates']), 1):
        if source_payload(original) != source_payload(d) or original.attachments != d.attachments or original.comparison_pdf_paths != d.comparison_pdf_paths:
            raise ValueError('탐지 원문과 선별 원문 불일치')
        validate_assessment(d)
        if entry['assessment'] != d.selection or entry['no'] != i or entry['law_name'] != d.meta.law_name or entry['publication_status'] != d.publication_status:
            raise ValueError('후보 원문·선별 목록 대응 불일치')
        if d.selection['status'] == 'priority':
            selected.append(d)
        elif entry['output_file']:
            raise ValueError('비선정 후보를 임의 발행함')
    if ledger['counts'] != {s: sum(d.selection['status'] == s for d in candidates) for s in LABELS}:
        raise ValueError('후보 분류 건수 불일치')
    if [asdict(d) for d in selected] != published:
        # asdict contains dates; snapshots store their ISO strings.
        if json.loads(json.dumps([asdict(d) for d in selected], default=str)) != published:
            raise ValueError('선정된 후보와 발행 스냅샷 불일치')
    entries = [e for e in ledger['candidates'] if e['assessment']['status'] == 'priority']
    if [e['output_file'] for e in entries] != [e['output_file'] for e in manifest['details']]:
        raise ValueError('후보 선별·개별본 연결 불일치')


def validate_delivery(base):
    from datetime import date
    from docx import Document
    from ..models import LawChangeMeta, LawChangeDetail, ArticleComparisonRow
    import fitz
    base = Path(base)
    errors = []
    def fail(message):
        errors.append(message)
    try:
        m = json.loads((base/'delivery_manifest.json').read_text())
        if m.get('schema') not in (1, 2) or m.get('reference_sha256') != REFERENCE_SHA256:
            fail('참고 양식 계약 불일치')
        snapshot = local_file(base, m['snapshot'])
        if file_hash(snapshot) != m['snapshot_sha256']:
            fail('원문 스냅샷 해시 불일치')
        rows = json.loads(snapshot.read_text())
        if m.get('schema') == 2:
            validate_selection_registry(base, m, rows)
        if not rows or not m['files']:
            fail('수집·발행 대상 없음. 배포 완료로 처리할 수 없음')
        if len(rows) != len(m['details']):
            fail('수집 원문·발행 목록 건수 불일치')
        policy = m['publication_policy']
        if policy == 'individual-and-digest':
            names = [d['output_file'] for d in m['details']]
            if not all(names) or len(set(names)) != len(names):
                fail('개별 안내서 누락 또는 여러 건을 임의로 합침')
            if rows and '목차.docx' not in [f['name'] for f in m['files']]:
                fail('목차 누락')
            elif rows:
                toc = Document(local_file(base, '목차.docx'))
                lines = [p.text for p in toc.paragraphs if p.text.strip()][2:]
                if len(lines) != len(m['details']) or any(d['law_name'] not in line for d, line in zip(m['details'], lines)):
                    fail('개별 안내서 목록과 목차 대응 불일치')
        try:
            reviews = json.loads((base/'delivery_review.json').read_text())
        except (OSError, ValueError):
            reviews = {}
        if reviews.get('manifest_sha256') != file_hash(base/'delivery_manifest.json'):
            fail('현재 파일·원문에 결합된 검토 기록 없음')
        if m.get('schema') == 2:
            sr = reviews.get('selection_review', {})
            if not (sr.get('status') == 'passed' and sr.get('reviewer') and sr.get('notes')):
                fail('후보 선정·조건부·참고·보류 사유 검토 미완료')
        if m.get('collection_warnings'):
            cr = reviews.get('collection_review', {})
            if not (cr.get('status') == 'passed' and cr.get('reviewer') and cr.get('notes')):
                fail('수집 경고·확인 범위 검토 미완료')
        listed = {f['name'] for f in m['files']}
        docs = {p.name for p in base.glob('*.docx')}
        if docs != {f['name'] for f in m['files'] if f['kind'] != 'attachment'}:
            fail('실제 DOCX와 발행 목록 불일치')
        if len(listed) != len(m['files']):
            fail('발행 파일명 중복')
        for raw, entry in zip(rows, m['details']):
            raw = dict(raw); meta = dict(raw['meta'])
            for k in ('announcement_date', 'effective_date', 'calendar_event_date'):
                if meta.get(k):
                    meta[k] = date.fromisoformat(meta[k])
            raw['meta'] = LawChangeMeta(**meta)
            raw['article_comparisons'] = [ArticleComparisonRow(**r) for r in raw['article_comparisons']]
            d = LawChangeDetail(**raw)
            if entry['source_sha256'] != digest(source_payload(d)) or entry['impact'] != impact_for_document(d):
                fail(f'원문·파급효과 대응 불일치: {d.meta.law_name}')
            if entry['impact_evidence'] != d.impact_evidence or entry.get('impact_correction', {}) != d.impact_correction:
                fail(f'호출·수정 기록과 생성 스냅샷 불일치: {d.meta.law_name}')
            if not entry['output_file']:
                if m.get('schema') == 2:
                    from .enrichment import valid_summary_evidence
                    if not valid_summary_evidence(d):
                        fail(f'요약의 실제 LLM 응답 증거 없음·실패·변조: {d.meta.law_name}')
                if policy == 'digest-only':
                    continue
                if policy == 'effective-family' and d.publication_status != '시행':
                    continue
                fail(f'개별본 연결 누락: {d.meta.law_name}'); continue
            if entry['output_file'] not in listed:
                fail(f'개별본 파일 목록 누락: {d.meta.law_name}'); continue
            if not source_limit(d) and not valid_evidence(d):
                fail(f'실제 LLM 응답 증거 없음·실패·변조: {d.meta.law_name}')
            doc = Document(local_file(base, entry['output_file']))
            if entry['impact'] not in [p.text for p in doc.paragraphs]:
                fail(f'검토 대상 파급효과와 DOCX 본문 불일치: {d.meta.law_name}')
            if source_limit(d):
                sr = reviews.get('files', {}).get(entry['output_file'], {}).get('semantic', {})
                if sr.get('source_limits_acknowledged') is not True:
                    fail(f'원문 확인 한계 미검토: {d.meta.law_name}')
        for name in {d['output_file'] for d in m['details'] if d['output_file']}:
            assigned = [(r, d) for r, d in zip(rows, m['details']) if d['output_file'] == name]
            document = Document(local_file(base, name))
            actual = [(row.cells[0].text, row.cells[1].text)
                      for table in document.tables for row in table.rows[1:]]
            expected = []
            for raw, entry in assigned:
                comparisons = raw['article_comparisons']
                if len(comparisons) != entry['comparison_rows']:
                    fail(f'대비표 원문 행 수 불일치: {name}')
                for row in comparisons:
                    expected.append(tuple(''.join(s[0] for s in row[side+'_segments'])
                                          if row.get(side+'_segments') else row.get(side+'_text') or ''
                                          for side in ('old', 'new')))
            if actual != expected:
                fail(f'대비표 본문 누락·원문 불일치: {name}')
        for f in m['files']:
            p = local_file(base, f['name'])
            if not p.is_file() or file_hash(p) != f['sha256']:
                fail(f'발행 파일 누락·해시 불일치: {f["name"]}'); continue
            if f['kind'] in ('guide', 'toc'):
                try:
                    validate_reference(p)
                except ValueError as exc:
                    fail(f'{p.name}: {exc}')
            if f['kind'] == 'summary':
                from ..docx_generator.summary_reference import validate_summary, SUMMARY_REFERENCE_SHA256
                if m.get('summary_reference_sha256') != SUMMARY_REFERENCE_SHA256:
                    fail('요약 참고 양식 계약 불일치')
                from ..docx_generator.summary_generator import _short_name, _change_date_str, _one_line_summary, _is_relevant, _legacy_is_relevant
                summary_details = []
                for original in rows:
                    raw = dict(original); meta = dict(raw['meta'])
                    for key in ('announcement_date', 'effective_date', 'calendar_event_date'):
                        if meta.get(key):
                            meta[key] = date.fromisoformat(meta[key])
                    raw['meta'] = LawChangeMeta(**meta)
                    raw['article_comparisons'] = [ArticleComparisonRow(**r) for r in raw['article_comparisons']]
                    summary_details.append(LawChangeDetail(**raw))
                full = [(i+1, _short_name(d), _change_date_str(d), _one_line_summary(d, use_llm=False))
                        for i,d in enumerate(summary_details)]
                relevance = _is_relevant if m.get('schema') == 2 else _legacy_is_relevant
                selected = [row for row,d in zip(full,summary_details) if relevance(d)]
                try:
                    validate_summary(p, expected_rows=full, expected_relevant=selected)
                except ValueError as exc:
                    fail(f'{p.name}: {exc}')
            if f['kind'] == 'attachment':
                continue
            r = reviews.get('files', {}).get(f['name'], {})
            if r.get('sha256') != f['sha256']:
                fail(f'현재 DOCX 해시에 대한 검토 없음: {p.name}')
            s, v = r.get('semantic', {}), r.get('visual', {})
            if not (s.get('status') == 'passed' and s.get('reviewer') and s.get('notes')
                    and all(s.get('checks', {}).get(k) is True for k in CHECKS)):
                fail(f'의무 주체·적용범위·시행일·원문 대조 미완료: {p.name}')
            if not (v.get('status') == 'passed' and v.get('reviewer') and v.get('rendered_from_sha256') == f['sha256']):
                fail(f'현재 DOCX의 전 페이지 시각 검수 미완료: {p.name}'); continue
            pdf = local_file(base, v.get('pdf', ''))
            if not pdf.is_file() or file_hash(pdf) != v.get('pdf_sha256'):
                fail(f'렌더 PDF 증거 없음·변경: {p.name}'); continue
            with fitz.open(pdf) as rendered:
                pages = len(rendered)
            images = v.get('images', [])
            if not pages or v.get('pages_reviewed') != pages or len(images) != pages:
                fail(f'전 페이지 PNG 검수 증거 불일치: {p.name}')
            if len({im.get('name') for im in images}) != len(images):
                fail(f'페이지 이미지 중복: {p.name}')
            for im in images:
                ip = local_file(base, im['name'])
                if not ip.is_file() or file_hash(ip) != im.get('sha256') or ip.read_bytes()[:8] != b'\x89PNG\r\n\x1a\n':
                    fail(f'렌더 PNG 증거 없음·변경: {p.name}')
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        fail(f'검증 입력 오류: {exc}')
    return {'status': 'blocked' if errors else 'ready', 'errors': errors}


def package_delivery(base, target):
    base, target = Path(base), Path(target)
    if target.exists():
        raise FileExistsError('기존 배포 ZIP은 덮어쓰지 않음')
    result = validate_delivery(base)
    (base/'delivery_validation.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    if result['status'] != 'ready':
        raise ValueError('배포 차단: ' + '; '.join(result['errors']))
    m = json.loads((base/'delivery_manifest.json').read_text())
    # Freeze bytes after validation; never include .env, caches or arbitrary directory contents.
    names = [f['name'] for f in m['files']] + ['delivery_manifest.json', 'delivery_review.json', 'delivery_validation.json', m['snapshot']]
    names += list(m.get('selection_files', {}))
    payload = {name: local_file(base, name).read_bytes() for name in names}
    for f in m['files']:
        import hashlib
        if hashlib.sha256(payload[f['name']]).hexdigest() != f['sha256']:
            raise ValueError('검증 후 파일 변경 감지')
    for name, expected in {m['snapshot']: m['snapshot_sha256'], **m.get('selection_files', {})}.items():
        if hashlib.sha256(payload[name]).hexdigest() != expected:
            raise ValueError('검증 후 원문·선별 기록 변경 감지')
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with ZipFile(target, 'x', ZIP_DEFLATED) as z:
            for name, data in payload.items():
                z.writestr(name, data)
        with ZipFile(target) as z:
            if z.testzip() is not None:
                raise ValueError('ZIP CRC 검증 실패')
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return target
