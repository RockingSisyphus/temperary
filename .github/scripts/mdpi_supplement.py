#!/usr/bin/env python3
import argparse, concurrent.futures as cf, csv, hashlib, json, os, re, sys, threading, time
from pathlib import Path
from urllib.parse import quote

import fitz
import requests
from rapidfuzz import fuzz

UA = "OpenAccessPaperRecovery/1.1 (+https://github.com/RockingSisyphus/temperary)"
MAP = {
    "a":"algorithms", "act":"actuators", "ani":"animals", "bdcc":"bdcc",
    "biomimetics":"biomimetics", "brainsci":"brainsci", "bs":"behavsci",
    "computation":"computation", "computers":"computers", "data":"data",
    "diagnostics":"diagnostics", "e":"entropy", "educsci":"education",
    "electronics":"electronics", "eng":"eng", "fi":"futureinternet",
    "healthcare":"healthcare", "ijms":"ijms", "info":"information",
    "inventions":"inventions", "jcm":"jcm", "jintelligence":"jintelligence",
    "jmmp":"jmmp", "journalmedia":"journalmedia", "languages":"languages",
    "make":"machines", "math":"mathematics", "met":"metals", "mti":"mti",
    "polym":"polymers", "robotics":"robotics", "rs":"remotesensing",
    "s":"sensors", "systems":"systems", "technologies":"technologies",
    "vehicles":"vehicles",
}
_tls = threading.local()

def session():
    if not hasattr(_tls, "s"):
        s = requests.Session()
        s.headers.update({"User-Agent":UA, "Accept":"application/pdf,text/html;q=0.8,*/*;q=0.5"})
        _tls.s = s
    return _tls.s

def norm(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    return " ".join(s.split())

def article_meta(row):
    doi = row["doi"].lower()
    m = re.fullmatch(r"10\.3390/([a-z]+)(\d+)", doi)
    if not m:
        raise ValueError("bad_mdpi_doi")
    code, digits = m.groups()
    slug = MAP.get(code, code)
    rec = {}
    try:
        r = session().get(f"https://api.crossref.org/works/{quote(doi, safe='')}", timeout=(10,25))
        if r.ok:
            rec = r.json().get("message", {})
    except Exception:
        pass
    vol_s = str(rec.get("volume") or "").strip()
    issue_s = str(rec.get("issue") or "").strip()
    vol = int(re.search(r"\d+", vol_s).group()) if re.search(r"\d+", vol_s) else None
    article = None
    for key in ("article-number", "page"):
        val = rec.get(key)
        if val:
            mm = re.search(r"\d+", str(val))
            if mm:
                article = int(mm.group())
                break
    if vol is None:
        for n in (2,1):
            v = int(digits[:n])
            if 1 <= v <= 99:
                vol = v
                break
    if article is None:
        rest = digits[len(str(vol)):]
        if issue_s:
            issue = int(re.search(r"\d+", issue_s).group())
            pref = f"{issue:02d}"
            if rest.startswith(pref):
                rest = rest[len(pref):]
        elif len(rest) > 4:
            rest = rest[2:]
        article = int(rest)
    return code, slug, vol, article, rec

def validate_pdf(data, row):
    if not data.startswith(b"%PDF-") or len(data) < 15000:
        return False, "not_pdf", {}
    try:
        doc = fitz.open(stream=data, filetype="pdf")
        pages = doc.page_count
        text = "\n".join(doc[i].get_text("text") for i in range(min(5, pages)))
        doc.close()
    except Exception as e:
        return False, f"pdf_parse:{type(e).__name__}", {}
    if pages < 1:
        return False, "zero_pages", {}
    nt, np = norm(row["title"]), norm(text)
    score = fuzz.token_set_ratio(nt, np)
    toks = [x for x in nt.split() if len(x) >= 4]
    overlap = sum(x in np for x in toks) / max(1, len(toks))
    doi_ok = row["doi"].lower().replace("https://doi.org/", "") in text.lower().replace("https://doi.org/", "")
    ok = doi_ok or score >= 72 or overlap >= 0.55
    return ok, "ok" if ok else "identity_mismatch", {"pages":pages,"title_score":score,"token_overlap":round(overlap,4),"doi_in_text":doi_ok}

def safe_name(row):
    title = re.sub(r"[^A-Za-z0-9._ -]+", "_", row["title"])
    title = re.sub(r"\s+", " ", title).strip()[:115]
    return f"{int(row['global_index']):04d}_{row['year']}_{title}.pdf"

def recover(row, out):
    result = {k:row.get(k) for k in ("global_index","section","year","title","doi","publisher","shard")}
    result.update(status="unresolved", source="", source_url="", filename="", size_bytes="", sha256="", validation="", errors="")
    errors, logs = [], []
    try:
        code, slug, vol, article, rec = article_meta(row)
        base = f"{slug}-{vol:02d}-{article:05d}"
        root = f"https://mdpi-res.com/d_attachment/{slug}/{base}/article_deploy/"
        urls = [root + base + ".pdf"] + [root + base + f"-v{i}.pdf" for i in range(2,8)] + [root + base + "-v1.pdf"]
        for url in urls:
            try:
                r = session().get(url, timeout=(10,45), allow_redirects=True)
                logs.append({"url":url,"status":r.status_code,"content_type":r.headers.get("content-type",""),"bytes":len(r.content)})
                if not r.ok:
                    continue
                ok, reason, meta = validate_pdf(r.content, row)
                logs[-1].update(result=reason, **meta)
                if not ok:
                    continue
                path = out / safe_name(row)
                path.write_bytes(r.content)
                result.update(status="downloaded_verified", source="mdpi-res", source_url=r.url,
                              filename=str(path), size_bytes=len(r.content),
                              sha256=hashlib.sha256(r.content).hexdigest(),
                              validation=json.dumps(meta, ensure_ascii=False))
                return result, logs
            except Exception as e:
                errors.append(f"{url}:{type(e).__name__}:{e}")
        result["errors"] = " | ".join(errors[-8:])
        result["validation"] = json.dumps({"code":code,"slug":slug,"volume":vol,"article":article,"crossref_title":(rec.get('title') or [''])[0]}, ensure_ascii=False)
    except Exception as e:
        result["errors"] = f"metadata:{type(e).__name__}:{e}"
    return result, logs

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--input',required=True); ap.add_argument('--output',required=True); ap.add_argument('--year',type=int,required=True); ap.add_argument('--workers',type=int,default=8); args=ap.parse_args()
    rows=json.loads(Path(args.input).read_text(encoding='utf-8'))
    rows=[r for r in rows if int(r['year'])==args.year and str(r.get('doi') or '').lower().startswith('10.3390/')]
    root=Path(args.output); pdf=root/'pdfs'/str(args.year); rep=root/'reports'; pdf.mkdir(parents=True,exist_ok=True); rep.mkdir(parents=True,exist_ok=True)
    results=[]; all_logs=[]
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        fut={ex.submit(recover,r,pdf):r for r in rows}
        for i,f in enumerate(cf.as_completed(fut),1):
            res, logs=f.result(); results.append(res)
            for x in logs: x.update(global_index=res['global_index'], title=res['title'], doi=res['doi'])
            all_logs.extend(logs)
            print(f"[{args.year} {i}/{len(rows)}] {res['status']} #{res['global_index']} {res['title'][:75]}", flush=True)
    results.sort(key=lambda x:int(x['global_index']))
    fields=list(results[0].keys()) if results else ['global_index','year','title','doi','status']
    with (rep/'manifest.csv').open('w',encoding='utf-8-sig',newline='') as h:
        w=csv.DictWriter(h,fieldnames=fields); w.writeheader(); w.writerows(results)
    (rep/'manifest.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    with (rep/'candidate_log.jsonl').open('w',encoding='utf-8') as h:
        for x in all_logs: h.write(json.dumps(x,ensure_ascii=False)+'\n')
    ok=sum(r['status']=='downloaded_verified' for r in results)
    summary={'year':args.year,'target_mdpi_records':len(rows),'downloaded_verified':ok,'unresolved':len(rows)-ok,'total_bytes':sum(int(r['size_bytes'] or 0) for r in results)}
    (rep/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False),flush=True)
if __name__=='__main__': main()
