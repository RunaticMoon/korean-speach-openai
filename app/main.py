"""Keep ``uvicorn app.main:create_app --factory`` working after the refactor."""

from speech_proxy.app import app, create_app

__all__ = ["app", "create_app"]
