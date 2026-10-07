from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    data_dir: Path = Path("./data")
    database_url: str = "sqlite:///./data/agent.db"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    def ensure_dirs(self): self.data_dir.mkdir(parents=True, exist_ok=True)

settings = Settings()
