"""fine-tunes: an A/B listening experiment harness for AI music generators."""
import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Config
from .models import db


def create_app(config=None):
    load_dotenv()
    settings = config or Config()

    app = FastAPI(title="fine-tunes")
    app.state.config = settings

    os.makedirs(settings.INSTANCE_DIR, exist_ok=True)
    os.makedirs(settings.AUDIO_STORAGE_DIR, exist_ok=True)

    db.init_app(settings)
    db.create_all()

    static_dir = os.path.join(settings.BASE_DIR, "static")
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    from .routes import router

    app.include_router(router)

    @app.middleware("http")
    async def database_session_middleware(request, call_next):
        token = db.begin_request()
        try:
            return await call_next(request)
        finally:
            db.end_request(token)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request, exc):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request, exc):
        return JSONResponse({"error": "invalid request"}, status_code=400)

    return app
