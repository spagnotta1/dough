"""Makes this directory a package so the test modules can import the helpers
that live beside them in conftest.py (`from .conftest import visit`).

pytest finds fixtures in a conftest without any of this; the package is only
needed for the plain functions — `visit`, `sign_in`, `assert_no_horizontal_overflow`
— which are imported by name.
"""
