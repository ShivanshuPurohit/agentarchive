"""Require a complete viewer in distributable builds; keep editable installs lightweight."""
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py
from setuptools.command.sdist import sdist


def require_viewer():
    if not Path("agentarchive/web/index.html").is_file():
        raise RuntimeError("Build the viewer first: npm --prefix viewer ci && npm --prefix viewer run build")


class Build(build_py):
    def run(self):
        if not self.editable_mode:
            require_viewer()
        super().run()


class Source(sdist):
    def run(self):
        require_viewer()
        super().run()


setup(cmdclass={"build_py": Build, "sdist": Source})
