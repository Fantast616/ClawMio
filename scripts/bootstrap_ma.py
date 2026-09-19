"""Compatibility entry point; implementation is also shipped in the CLI wheel."""
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from clawmio.provision import Provisioner, SetupError, main


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except SetupError as exc:
        print(str(exc),file=sys.stderr)
        raise SystemExit(1)
