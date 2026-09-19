"""Build a Linux release from a wheel and an explicit public-file allowlist."""
import hashlib
import io
from pathlib import Path
import tarfile
import tomllib

ROOT=Path(__file__).resolve().parents[1]


def main():
    version=tomllib.loads((ROOT/'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
    name=f'clawmio-{version}-linux'
    wheel=ROOT/f'dist/clawmio-{version}-py3-none-any.whl'
    if not wheel.exists():
        raise SystemExit('Run python -m build first.')
    output=ROOT/f'dist/{name}.tar.gz'
    files={
        wheel.name:(wheel,0o644),
        'install.sh':(ROOT/'deploy/linux-release/install.sh',0o755),
        'clawmio':(ROOT/'deploy/linux-release/clawmio',0o755),
        'README.txt':(ROOT/'deploy/linux-release/README.txt',0o644),
        'docs/cli.md':(ROOT/'docs/cli.md',0o644),
        'LICENSE':(ROOT/'LICENSE',0o644),
    }
    with tarfile.open(output,'w:gz') as archive:
        for relative,(source,mode) in files.items():
            content=source.read_bytes()
            if source!=wheel:
                content=content.replace(b'\r\n',b'\n')
            info=tarfile.TarInfo(name+'/'+relative)
            info.size=len(content)
            info.mode=mode
            archive.addfile(info,io.BytesIO(content))
    digest=hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix+'.sha256').write_text(f'{digest}  {output.name}\n',encoding='ascii')
    print(f'Created {output.name} and SHA-256 checksum; requires Python 3.11+ and network.')


if __name__=='__main__':
    main()
