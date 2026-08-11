#!/usr/bin/env python3
import csv, hashlib, json, re
from pathlib import Path
from urllib.parse import urljoin

import fitz
import requests
from bs4 import BeautifulSoup
from rapidfuzz import fuzz

TARGETS = {80, 143, 199, 385, 426, 461, 477}
SLUGS = {
    'bdcc': ['bdcc'], 'data': ['data'], 'languages': ['languages'],
    'vehicles': ['vehicles'], 'make': ['machines', 'make'],
}
UA = 'OpenAccessPaperRecovery/1.2 (+https://github.com/RockingSisyphus/temperary)'
S = requests.Session(); S.headers.update({'User-Agent': UA, 'Accept': 'application/pdf,text/html,*/*'})

def norm(s):
    return ' '.join(re.sub(r'[^a-z0-9]+', ' ', (s or '').lower()).split())

def validate(data, row):
    meta = {'bytes': len(data)}
    if not data.startswith(b'%PDF-') or len(data) < 15000:
        return False, 'not_pdf', meta
    try:
        doc = fitz.open(stream=data, filetype='pdf')
        meta['pages'] = doc.page_count
        text = '\n'.join(doc[i].get_text('text') for i in range(min(6, doc.page_count)))
        doc.close()
    except Exception as exc:
        return False, f'pdf_parse:{type(exc).__name__}', meta
    nt, np = norm(row['title']), norm(text)
    score = fuzz.token_set_ratio(nt, np)
    toks = [x for x in nt.split() if len(x) >= 4]
    overlap = sum(x in np for x in toks) / max(1, len(toks))
    doi_compact = re.sub(r'[^a-z0-9]', '', row['doi'].lower())
    text_compact = re.sub(r'[^a-z0-9]', '', text.lower())
    doi_ok = doi_compact in text_compact
    meta.update(title_score=round(score, 2), token_overlap=round(overlap, 4), doi_in_text=doi_ok)
    ok = doi_ok or score >= 72 or overlap >= 0.55
    return ok, 'ok' if ok else 'identity_mismatch', meta

def direct_urls(row):
    m = re.fullmatch(r'10\.3390/([a-z]+)(\d+)', row['doi'].lower())
    if not m:
        return []
    code, digits = m.groups()
    if len(digits) < 7:
        return []
    volume = int(digits[:-6]); article = int(digits[-4:])
    urls = []
    for slug in SLUGS.get(code, [code]):
        base = f'{slug}-{volume:02d}-{article:05d}'
        root = f'https://mdpi-res.com/d_attachment/{slug}/{base}/article_deploy/'
        urls.extend([root + base + '.pdf'] + [root + base + f'-v{i}.pdf' for i in range(2, 11)] + [root + base + '-v1.pdf'])
    return urls

def landing_urls(row, logs):
    urls = []
    try:
        response = S.get('https://doi.org/' + row['doi'], timeout=(10, 35), allow_redirects=True)
        logs.append({'url': 'https://doi.org/' + row['doi'], 'status': response.status_code, 'content_type': response.headers.get('content-type',''), 'bytes': len(response.content)})
        if response.ok and 'html' in response.headers.get('content-type','').lower():
            soup = BeautifulSoup(response.text, 'html.parser')
            for tag in soup.select('a[href], link[href], meta[content]'):
                raw = tag.get('href') or tag.get('content') or ''
                if '.pdf' in raw.lower() or '/pdf' in raw.lower():
                    urls.append(urljoin(response.url, raw))
    except Exception as exc:
        logs.append({'url': 'https://doi.org/' + row['doi'], 'error': f'{type(exc).__name__}:{exc}'})
    return urls

def filename(row):
    title = re.sub(r'[^A-Za-z0-9._ -]+', '_', row['title'])
    title = re.sub(r'\s+', ' ', title).strip()[:120]
    return f"{int(row['global_index']):04d}_{row['year']}_{title}.pdf"

def main():
    papers = json.loads(Path('oa_recovery/papers.json').read_text(encoding='utf-8'))
    rows = [r for r in papers if int(r['global_index']) in TARGETS]
    out = Path('mdpi_retry_output'); pdfdir = out / 'pdfs'; rep = out / 'reports'
    pdfdir.mkdir(parents=True, exist_ok=True); rep.mkdir(parents=True, exist_ok=True)
    results, all_logs = [], []
    for pos, row in enumerate(rows, 1):
        logs, seen, found = [], set(), None
        candidates = direct_urls(row)
        for stage in (0, 1):
            if stage == 1:
                candidates.extend(landing_urls(row, logs))
            for url in candidates:
                if url in seen: continue
                seen.add(url)
                try:
                    response = S.get(url, timeout=(10, 50), allow_redirects=True)
                    record = {'url': url, 'final_url': response.url, 'status': response.status_code, 'content_type': response.headers.get('content-type',''), 'bytes': len(response.content)}
                    logs.append(record)
                    if not response.ok: continue
                    ok, reason, meta = validate(response.content, row)
                    record.update(result=reason, **meta)
                    if not ok: continue
                    path = pdfdir / filename(row); path.write_bytes(response.content)
                    found = {'global_index': row['global_index'], 'year': row['year'], 'title': row['title'], 'doi': row['doi'], 'publisher': row.get('publisher',''), 'status': 'downloaded_verified', 'source': 'mdpi-retry', 'source_url': response.url, 'filename': str(path), 'size_bytes': len(response.content), 'sha256': hashlib.sha256(response.content).hexdigest(), 'validation': json.dumps(meta, ensure_ascii=False), 'errors': ''}
                    break
                except Exception as exc:
                    logs.append({'url': url, 'error': f'{type(exc).__name__}:{exc}'})
            if found: break
        if not found:
            found = {'global_index': row['global_index'], 'year': row['year'], 'title': row['title'], 'doi': row['doi'], 'publisher': row.get('publisher',''), 'status': 'unresolved', 'source': '', 'source_url': '', 'filename': '', 'size_bytes': '', 'sha256': '', 'validation': '', 'errors': 'all_candidates_exhausted'}
        for item in logs: item.update(global_index=row['global_index'], title=row['title'], doi=row['doi'])
        all_logs.extend(logs); results.append(found)
        print(f"[{pos}/{len(rows)}] {found['status']} #{row['global_index']} {row['title'][:80]}", flush=True)
    results.sort(key=lambda x: int(x['global_index']))
    (rep/'manifest.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    with (rep/'manifest.csv').open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(results[0].keys())); writer.writeheader(); writer.writerows(results)
    with (rep/'candidate_log.jsonl').open('w', encoding='utf-8') as handle:
        for item in all_logs: handle.write(json.dumps(item, ensure_ascii=False) + '\n')
    ok = sum(r['status'] == 'downloaded_verified' for r in results)
    summary = {'target_records': len(results), 'downloaded_verified': ok, 'unresolved': len(results)-ok, 'total_bytes': sum(int(r['size_bytes'] or 0) for r in results)}
    (rep/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False), flush=True)
if __name__ == '__main__': main()
