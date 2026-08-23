from fastapi import FastAPI

from app.api.routes import router
from app.config import get_settings
from app.db.base import Base, engine

settings = get_settings()

app = FastAPI(title=settings.app_name)
app.include_router(router, prefix="/api")


@app.on_event("startup")
def on_startup() -> None:
    Base.metadata.create_all(bind=engine)
