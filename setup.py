from setuptools import setup


requires = [line for line in open('requirements.txt').read().strip().split('\n') if not line.startswith('http')]

version = '0.0.1'
# noinspection PyBroadException
try:
    import subprocess
    p = subprocess.Popen(['git', 'rev-parse', '--short', 'HEAD'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    commit, _ = p.communicate()
    if commit:
        version += '+git.' + commit.decode('UTF-8').strip()
except Exception:
    pass

setup(
    name='pylava',
    author='Pandentia',
    description='A lavalink interface for discord.py bots',
    license='MIT',
    packages=['pylava'],
    install_requires=requires,
    version=version,
)
