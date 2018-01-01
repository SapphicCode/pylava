from setuptools import setup


setup(
    name='pylava',
    author='Pandentia',
    description='A lavalink interface for discord.py bots',
    license='MIT',
    packages=['pylava'],
    install_requires=['aiohttp', 'websockets']  # ignoring discord.py rewrite for now
)
