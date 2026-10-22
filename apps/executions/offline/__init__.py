"""Offline execution support (Phase 10): the package a device downloads and the synchronization it uploads.

Everything here reuses the Phase 9 services (``start_or_resume``, ``save_answers``, ``submit``); there is no second execution
model.  The server stays authoritative: every operation is re-authorized, re-validated and re-evaluated at sync time.
"""
