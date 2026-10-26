#!/usr/bin/env python3
"""Read-only report, no inference, no keys in output, no publications."""
import argparse,json,subprocess,urllib.request

def run(*args):return subprocess.run(['gcloud',*args],check=True,capture_output=True,text=True).stdout.strip()

def main():
 p=argparse.ArgumentParser();p.add_argument('--url');p.add_argument('--expected');p.add_argument('--project',default='winged-ratio-377512');p.add_argument('--region',default='europe-north1');args=p.parse_args()
 url=args.url or run('run','services','describe','finland-news-bot','--project='+args.project,'--region='+args.region,'--format=value(status.url)')
 with urllib.request.urlopen(url+'/health',timeout=30) as r:health=json.load(r)
 print('Health:',health)
 if health.get('ok') is not True or (args.expected and health.get('version')!=args.expected):raise RuntimeError('Candidate health/version mismatch')
 token=run('secrets','versions','access','latest','--secret=cron-secret','--project='+args.project)
 request=urllib.request.Request(url+'/admin/recovery-report',data=b'{}',headers={'Content-Type':'application/json','X-Cron-Secret':token})
 with urllib.request.urlopen(request,timeout=120) as r:report=json.load(r)
 if not report or any(v.get('patch')!='recovery-20261026' for v in report.values()):raise RuntimeError('Recovery endpoint not active')
 print(json.dumps(report,ensure_ascii=False,indent=2,default=str))
 for job in ('finland-news-collect','finland-events-collect','finland-news-dispatch'):
  try:print(job,run('scheduler','jobs','describe',job,'--project='+args.project,'--location=europe-west1','--format=yaml(state,schedule,lastAttemptTime,status)'))
  except Exception:print(job+': could not read job state')

if __name__=='__main__':
 try:main()
 except Exception as error:print('Diagnostic failed: '+type(error).__name__+'. Candidate has not been confirmed; check permissions, logs and service readiness.');raise SystemExit(1)
