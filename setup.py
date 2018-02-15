import os

from setuptools import setup

def long_description():
  os.system('pandoc --from=markdown --to=rst --output=README.rst README.md')
  readme_fn = os.path.join(os.path.dirname(__file__), 'README.rst')
  if os.path.exists(readme_fn):
    with open(readme_fn) as f:
      return f.read()
  else:
    return 'not available'

setup(
  name='gnomecast',
  version=__import__('gnomecast').__version__,
  description='A native Linux GUI for Chromecasting local files.',
  long_description=long_description(),
  author='Derek Anderson',
  author_email='public@kered.org',
  url='https://github.com/keredson/gnomecast',
  packages=['gnomecast'],
  py_modules=['gnomecast'],
  classifiers=[
    'Development Status :: 4 - Beta',
    'Intended Audience :: Developers',
    'License :: OSI Approved :: MIT License',
    'Operating System :: OS Independent',
    'Programming Language :: Python',
    'Programming Language :: Python :: 3',
  ],
  install_requires=['pychromecast','bottle','pycaption'],
  package_data={'gnomecast': ['gnomecast/cast.png']},
  package_dir={'gnomecast': 'gnomecast'},
  include_package_data=True,
)


