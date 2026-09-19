from pathlib import Path
import os


def assets():
    bundled = Path(__file__).resolve().parent / 'resources'
    return bundled if bundled.is_dir() else Path(__file__).resolve().parent.parent


def home_path(value=None):
    return Path(value or os.getenv('CLAWMIO_HOME') or Path.home() / '.clawmio').expanduser().resolve()
