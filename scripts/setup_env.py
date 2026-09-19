"""Create local configuration without printing credentials. Standard library only."""
import argparse
import os
from pathlib import Path
import secrets

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description='Create .env with random admin credentials; never overwrite an existing file.')
    parser.parse_args()
    text=(ROOT/'.env.example').read_text(encoding='utf-8')
    text=text.replace('replace-with-a-long-password',secrets.token_urlsafe(24))
    text=text.replace('replace-with-a-random-secret',secrets.token_hex(32))
    try:
        fd=os.open(ROOT/'.env',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError:
        print('.env already exists; it has not been changed.')
        return 0
    with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as file:
        file.write(text)
    print('Created .env. Fill DASHSCOPE_API_KEY, BAILIAN_WORKSPACE_ID and BAILIAN_REGION.')
    print('Then run scripts/bootstrap_ma.py to preview resource setup; see docs/bootstrap.md.')
    print('Read ADMIN_PASSWORD in .env to log in. Do not share or commit this file.')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
