"""The Dough application package.

Everything in here is importable without a Flask app existing yet. Nothing at
import time may touch `current_app`, `request`, the database, or the network --
`app.py` imports this package at module scope, so an import-time side effect
would run before `create_app()` had built anything to have a side effect on.
"""
