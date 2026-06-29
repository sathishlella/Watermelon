"""Central configuration, loaded from the environment / .env file."""
import os

from dotenv import load_dotenv

load_dotenv()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_OWNER = os.getenv("GITHUB_OWNER", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")

# LLM provider — any OpenAI-compatible API (OpenAI, Groq, ...). The key is read
# from OPENAI_API_KEY, then GROQ_API_KEY, then the generic LLM_API_KEY alias.
# LLM_BASE_URL + LLM_MODEL select the provider/model.
LLM_API_KEY = (os.getenv("OPENAI_API_KEY", "")
               or os.getenv("GROQ_API_KEY", "")
               or os.getenv("LLM_API_KEY", ""))
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "llama-3.3-70b-versatile")

MAX_SYNTHESIS_ATTEMPTS = int(os.getenv("MAX_SYNTHESIS_ATTEMPTS", "3"))
MEMORY_DIR = os.getenv("MEMORY_DIR", "memory_data")


def require_env() -> None:
    """Fail fast with a clear message if required secrets are missing."""
    missing = [
        name
        for name, val in {
            "GITHUB_TOKEN": GITHUB_TOKEN,
            "GITHUB_OWNER": GITHUB_OWNER,
            "GITHUB_REPO": GITHUB_REPO,
            "OPENAI_API_KEY/GROQ_API_KEY": LLM_API_KEY,
        }.items()
        if not val
    ]
    if missing:
        raise SystemExit(
            "Missing required environment variables: "
            + ", ".join(missing)
            + "\nCopy .env.example to .env and fill them in."
        )
