"""Per-request identity shared by the HTTP layer and the audit log."""
from contextvars import ContextVar

# Short OAuth client identifier of the caller; never a token or credential.
current_client: ContextVar[str] = ContextVar("telegram_oauth_client", default="local")
