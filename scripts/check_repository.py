"""Heuristic publication guard: inspect Git index bytes, never print secrets."""
import argparse
import re
import subprocess
from pathlib import PurePosixPath


def git(*args):
    return subprocess.check_output(['git', *args])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--staged',action='store_true')
    args=parser.parse_args()
    names=git('diff','--cached','--name-only','--diff-filter=ACMR','-z') if args.staged else git('ls-files','-z')
    failures=[]
    paths=[p.decode('utf-8') for p in names.split(b'\0') if p]
    for path in paths:
        name=PurePosixPath(path).name
        if (path.startswith(('data/','artifacts/','.venv/','.venv-linux/')) or
            (name.startswith('.env') and name!='.env.example') or
            name=='local-access.txt' or re.search(r'\.(?:db|sqlite3?|log|pem|key|zip)(?:-|$)',name)):
            failures.append((path,'private/generated file'))
        body=git('show',':'+path)
        for label,pattern in (
            ('workspace API key',rb'sk-ws-[A-Za-z0-9_.-]{25,}'),
            ('Bot token',rb'cb_[A-Za-z0-9_-]{40,}'),
            ('private key',rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
        ):
            if re.search(pattern,body):
                failures.append((path,label))
    for path,label in failures:
        print(f'BLOCKED {path}: {label}')
    print(f'Checked {len(paths)} index files; {len(failures)} findings. Heuristic checks do not replace review.')
    return bool(failures)


if __name__=='__main__':
    raise SystemExit(main())
