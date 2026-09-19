"""Copy only public runtime assets into wheels; never bundle local configuration."""
from pathlib import Path
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildWithAssets(build_py):
    def run(self):
        super().run()
        root = Path(__file__).parent
        target = Path(self.build_lib) / 'clawmio/resources'
        for name in (
            'static/index.html', 'static/app.js', 'static/style.css',
            'docs/agent-system-prompt.txt',
            'skills/claw-bot-self-service/SKILL.md',
            'skills/claw-bot-self-service/scripts/bot_api.py',
        ):
            dest = target / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / name, dest)


setup(cmdclass={'build_py': BuildWithAssets})
