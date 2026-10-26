#!/usr/bin/env python3
"""Read-only, redacted history inventory. Run from any directory.

Scans every blob and commit object reachable from all locally available refs
and HEAD, including deleted files. Reports file, kind and commit only, never
matched values or source lines. Does not fetch, rewrite history, or determine
whether a candidate is a real credential. Review classifications before release.
"""
import json, re, subprocess
from collections import defaultdict
from pathlib import Path
repo = str(Path(__file__).resolve().parent.parent)
def git(*args):
    return subprocess.check_output(['git', '-C', repo, *args])
patterns = {
 'slack-token-like': rb'(?:xox[baprs]-[A-Za-z0-9-]{8,}|xapp-[A-Za-z0-9-]{8,})',
 'api-key-like': rb'(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AIza[A-Za-z0-9_-]{30,})',
 'private-key': rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
 'credential-assignment': rb'''(?i)(?:signing[_-]?secret|api[_-]?key|client[_-]?secret|password|token)[\w .-]*\s*[:=]\s*["'][A-Za-z0-9_+/=-]{20,}["']''',
 'slack-id-like': rb'\b[ATUCDGB][A-Z0-9]{8,}\b',
 'absolute-user-path': rb'/Users/[^/\s"\x27]+',
 'ipv4': rb'\b(?:\d{1,3}\.){3}\d{1,3}\b',
 'email': rb'\b[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b',
 'slack-project-uuid': rb'"project_id"\s*:\s*"[0-9a-f-]{36}"',
}
compiled={k: re.compile(v) for k,v in patterns.items()}
commits=git('rev-list','--all','HEAD').decode().splitlines()
blobs={}; findings=defaultdict(list)
for commit in commits:
    for entry in git('ls-tree','-rz','--full-tree',commit).split(b'\0'):
        if not entry: continue
        meta,path=entry.split(b'\t',1)
        mode,kind,oid=meta.split()
        if kind!=b'blob': continue
        if oid not in blobs:
            data=git('cat-file','blob',oid.decode())
            blobs[oid]=[key for key,rx in compiled.items() if rx.search(data)]
        for key in blobs[oid]: findings[(path.decode(),key)].append(commit)
    # Commit objects include messages and author/committer metadata.
    obj=git('cat-file','commit',commit)
    for key,rx in compiled.items():
        if rx.search(obj): findings[('<commit metadata/message>',key)].append(commit)
report={'head':git('rev-parse','HEAD').decode().strip(),'commits':len(commits),
        'unique_blobs':len(blobs),'scope':'git rev-list --all HEAD; every commit tree and commit object',
        'findings':[{'path':p,'kind':k,'example_commit':v[-1],'commit_count':len(set(v))}
                    for (p,k),v in sorted(findings.items())]}
print(json.dumps(report,indent=2))
