
import setuptools

# we define the license string like this to be backwards compatible to setuptools<77
setuptools.setup(
    license="AGPL-3.0-or-later",
    ext_modules=[setuptools.Extension("octoprint_sdwire._vfatdir", ["src/_vfatdir.c"])],
)

