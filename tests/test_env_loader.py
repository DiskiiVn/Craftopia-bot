import os
from pathlib import Path

from env_loader import load_dotenv


def test_loads_env_without_overwriting_existing(tmp_path: Path):
    path = tmp_path / ".env"
    path.write_text("SERVER_NAME='Craftopia'\nGEMINI_MODEL=gemini-test\n", encoding="utf-8")
    old_server_name = os.environ.get("SERVER_NAME")
    old_model = os.environ.get("GEMINI_MODEL")
    try:
        os.environ["SERVER_NAME"] = "Existing"
        os.environ.pop("GEMINI_MODEL", None)
        assert load_dotenv(path)
        assert os.environ["SERVER_NAME"] == "Existing"
        assert os.environ["GEMINI_MODEL"] == "gemini-test"
    finally:
        if old_server_name is None:
            os.environ.pop("SERVER_NAME", None)
        else:
            os.environ["SERVER_NAME"] = old_server_name
        if old_model is None:
            os.environ.pop("GEMINI_MODEL", None)
        else:
            os.environ["GEMINI_MODEL"] = old_model
