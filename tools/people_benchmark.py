#!/usr/bin/env python3
"""Prepare a bounded people benchmark; submitting GPU jobs requires --submit.

Results are evidence, not a model ranking until humans review the artifacts.
"""
import argparse
import json
import urllib.request
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8765')
    parser.add_argument('--model', action='append', required=True)
    parser.add_argument('--case', action='append', dest='cases')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--submit', action='store_true')
    args = parser.parse_args()
    suite = json.loads((Path(__file__).resolve().parents[1]/'aladin/people_cases.json').read_text())
    cases = [c for c in suite['cases'] if not args.cases or c['id'] in args.cases]
    if not cases or (args.cases and set(args.cases) - {c['id'] for c in cases}):
        parser.error('Unknown case; see aladin/people_cases.json')
    if args.out.exists():
        parser.error('--out already exists; use another filename to preserve previous receipts')
    requests = [dict(case=c['id'], body=dict(prompt=c['prompt'], model=model,
                size=suite['size'], seed=suite['seed'], images=suite['images']))
                for model in dict.fromkeys(args.model) for c in cases]
    result = dict(version=suite['version'], status='prepared', requests=requests)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    def save():
        temp = args.out.with_suffix(args.out.suffix+'.tmp')
        temp.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
        temp.replace(args.out)
    save()
    print(f'{len(requests)} jobs / {sum(r["body"]["images"] for r in requests)} images; manifest {args.out}')
    if not args.submit:
        return
    result['status'] = 'submitting'
    for item in requests:
        request = urllib.request.Request(args.url.rstrip('/')+'/api/v1/images',
                    data=json.dumps(item['body']).encode(), headers={'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                job = json.load(response)
            item['job_id'] = job['id']
            item['created'] = job['created']
            print(item['case'], item['body']['model'], job['id'], flush=True)
        except Exception as error:
            item['error'] = str(error)
            result['status'] = 'submission_interrupted'
            save()
            raise
        save()
    result['status'] = 'submitted_not_reviewed'
    save()


if __name__ == '__main__':
    main()
