from datetime import datetime
from sqlalchemy import create_engine, String, Text, Integer, DateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from .config import settings

settings.ensure_dirs(); engine = create_engine(settings.database_url, connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
class Base(DeclarativeBase): pass
class Document(Base):
    __tablename__="documents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url: Mapped[str] = mapped_column(String(2048), unique=True, index=True)
    title: Mapped[str | None] = mapped_column(String(500)); status: Mapped[str] = mapped_column(String(20), default="failed", index=True)
    summary_json: Mapped[str | None] = mapped_column(Text); categories: Mapped[str | None] = mapped_column(Text); keywords: Mapped[str | None] = mapped_column(Text)
    markdown_path: Mapped[str | None] = mapped_column(String(2048)); error: Mapped[str | None] = mapped_column(Text); error_stage: Mapped[str | None] = mapped_column(String(50)); retryable: Mapped[int] = mapped_column(Integer, default=1)
    duration_ms: Mapped[int | None] = mapped_column(Integer); created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow); updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
def init_db(): Base.metadata.create_all(engine)
