"""Application (use-case) layer.

Thin orchestration that composes the domain services (pipeline, normalization,
scoring) and infrastructure (db, storage, workers) for the API/interface layer,
keeping HTTP handlers free of orchestration logic.
"""
