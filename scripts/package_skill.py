"""Build a MA-uploadable ZIP using an explicit, credential-free file allowlist."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

ROOT=Path(__file__).resolve().parents[1]


def main():
    output=ROOT/'artifacts/claw-bot-self-service.zip'
    output.parent.mkdir(exist_ok=True)
    source=ROOT/'skills/claw-bot-self-service'
    with ZipFile(output,'w',ZIP_DEFLATED) as archive:
        for name in ('SKILL.md','scripts/bot_api.py'):
            archive.write(source/name,name)
    print('Created artifacts/claw-bot-self-service.zip (SKILL.md at ZIP root).')


if __name__=='__main__':
    main()
