from pathlib import Path

from fastapi.testclient import TestClient

from invoice_agent.config import Settings
from invoice_agent.main import Services, create_app


class PassiveWorker:
    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


def test_health_endpoint(tmp_path: Path) -> None:
    settings = Settings(
        telegram_bot_token="token",
        telegram_allowed_chat_ids=(123,),
        telegram_webhook_secret="secret",
        openai_api_key="key",
        openai_base_url="https://api.openai.com/v1",
        openai_model="gpt-5.3-codex",
        database_path=tmp_path / "invoice.db",
        output_dir=tmp_path / "generated",
    )
    services = Services(None, None, None, PassiveWorker())

    with TestClient(create_app(settings, services)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
