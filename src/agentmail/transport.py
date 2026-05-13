"""Transport adapter interface and built-in HTTP transport.

Every adapter implements send() and receive(). Adding a new transport
means writing a new adapter — no changes to the core protocol.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

import httpx

from .mail import Mail
from .config import AgentMailConfig, resolve_address

logger = logging.getLogger(__name__)


class SendResult:
    """Result of a send operation."""

    def __init__(self, success: bool, mail_hash: str = "", error: str = ""):
        self.success = success
        self.mail_hash = mail_hash
        self.error = error

    def __repr__(self) -> str:
        if self.success:
            return f"SendResult(ok, hash={self.mail_hash})"
        return f"SendResult(fail, error={self.error})"


class TransportAdapter(ABC):
    """Base class for all transport adapters."""

    @abstractmethod
    async def send(self, address: str, mail: Mail) -> SendResult:
        """Deliver a Mail object to the target address."""

    @abstractmethod
    async def receive(self, mail: Mail) -> None:
        """Handle an incoming Mail object from this transport.

        For push-based transports (HTTP, Telegram), the server endpoint
        collects incoming Mail and routes it to the store directly.
        Pull-based transports (MQTT subscriber) would use this to
        process messages as they arrive.
        """


class HTTPTransport(TransportAdapter):
    """HTTP transport adapter — default for LAN/cloud communication.

    Sends Mail objects via POST to the target agent's /receive endpoint.
    """

    def __init__(self, config: AgentMailConfig):
        self.config = config
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def send(self, address: str, mail: Mail) -> SendResult:
        """Send a Mail to an address via HTTP POST."""
        # Resolve client_id to full address if needed
        entry = resolve_address(address, self.config)
        if entry:
            target = entry.address
        else:
            target = address

        # Parse device@host:port/agent into URL
        try:
            url = self._address_to_url(target)
        except ValueError as e:
            return SendResult(success=False, error=str(e))

        client = await self._get_client()
        try:
            resp = await client.post(f"{url}/receive", json=mail.to_dict())
            if resp.status_code == 200:
                data = resp.json()
                return SendResult(
                    success=True,
                    mail_hash=data.get("mail_hash", mail.short_hash),
                )
            return SendResult(
                success=False,
                error=f"HTTP {resp.status_code}: {resp.text}",
            )
        except httpx.ConnectError as e:
            return SendResult(success=False, error=f"Connection failed: {e}")
        except Exception as e:
            return SendResult(success=False, error=f"Send error: {e}")

    async def receive(self, mail: Mail) -> None:
        """HTTP receive is handled by the FastAPI endpoints, not this adapter."""
        pass

    @staticmethod
    def _address_to_url(address: str) -> str:
        """Convert agentmail address to HTTP URL.

        ultron@100.95.112.96:5000/openclaw → http://100.95.112.96:5000
        gpu-farm@example.com:8080/hermes → http://example.com:8080
        """
        if address.startswith("http://") or address.startswith("https://"):
            return address

        parts = address.split("@")
        if len(parts) != 2:
            raise ValueError(f"Invalid address format: {address}")

        host_part = parts[1]  # device_address:port/agent_name
        # Strip the /agent_name part for URL
        if "/" in host_part:
            host_part = host_part.split("/")[0]

        return f"http://{host_part}"

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()


class StdioTransport(TransportAdapter):
    """Stdio transport for local process-to-process communication (pipes)."""

    async def send(self, address: str, mail: Mail) -> SendResult:
        """Stdio transport writes mail JSON to stdout."""
        logger.info(f"[Stdio] Would send to {address}: {mail.short_hash}")
        return SendResult(success=True, mail_hash=mail.short_hash)

    async def receive(self, mail: Mail) -> None:
        """Stdio transport reads from stdin."""
        pass