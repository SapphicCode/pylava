from setuptools import setup


requires = [line for line in open('requirements.txt').read().strip().split('\n') if not line.startswith('http')]

setup(
    name='pylava',
    author='Pandentia',
    description='A lavalink interface for discord.py bots',
    license='MIT',
    packages=['pylava'],
    install_requires=requires
)
