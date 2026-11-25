"""
Groq API key rotation client.

Why rotation?
  Free Groq keys have per-minute token limits (~6000 TPM on free tier).
  By cycling through N keys we multiply that limit by N.
  If a key hits its rate limit (429), we immediately move to the next one.
  Keys are tried round-robin; on 429 the failed key is back-off'd for 60 s.
"""
import asyncio
import time
import logging
from itertools import cycle
from typing import Optional

from groq import AsyncGroq
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = logging.getLogger(__name__)


class GroqModelUnavailableError(RuntimeError):
    """Raised when a configured Groq model does not exist / was deprecated."""


def _default_model() -> str:
    from app.core.config import settings
    return settings.GROQ_MAIN_MODEL


class GroqRotationClient:
    def __init__(self, api_keys: list[str]):
        if not api_keys:
            raise ValueError("At least one Groq API key required")
        self.keys     = api_keys
        self.clients  = [AsyncGroq(api_key=k) for k in api_keys]
        self._cycle   = cycle(range(len(self.clients)))
        self._backoff: dict[int, float] = {}   # key_index -> cooldown_until
        self._lock    = asyncio.Lock()
        # Client-side throttle: tokens sent with each key during the last 60 s,
        # as (timestamp, tokens) entries, so a key is not used beyond its
        # per-minute token limit (GROQ_TPM_PER_KEY) and Groq never has to refuse.
        self._usage: dict[int, list[list[float]]] = {i: [] for i in range(len(api_keys))}

    @staticmethod
    def _tpm_limit() -> int:
        from app.core.config import settings
        return int(getattr(settings, "GROQ_TPM_PER_KEY", 0) or 0)

    def _used_last_minute(self, idx: int, now: float) -> float:
        entries = [e for e in self._usage[idx] if now - e[0] < 60]
        self._usage[idx] = entries
        return sum(e[1] for e in entries)

    def _reserve(self, estimate: int) -> tuple[Optional[int], float, Optional[list]]:
        """
        Pick a key that is not cooling down and has room for `estimate` tokens
        in its current minute. Returns (idx, 0, entry) or (None, seconds_to_wait, None).
        """
        now, limit = time.monotonic(), self._tpm_limit()
        best_wait = float("inf")
        for _ in range(len(self.clients)):
            idx = next(self._cycle)
            cooldown = self._backoff.get(idx, 0) - now
            if cooldown > 0:
                best_wait = min(best_wait, cooldown)
                continue
            if limit <= 0:
                return idx, 0.0, None
            used = self._used_last_minute(idx, now)
            if used + estimate <= limit or not self._usage[idx]:
                entry = [now, float(estimate)]
                self._usage[idx].append(entry)
                return idx, 0.0, entry
            # wait until enough old entries leave the 60 s window
            freed, wait = used, 0.0
            for ts, tok in sorted(self._usage[idx]):
                freed -= tok
                wait = 60 - (now - ts)
                if freed + estimate <= limit:
                    break
            best_wait = min(best_wait, max(wait, 0.5))
        return None, best_wait, None

    def _next_available(self) -> int:
        """Return the next non-cooled-down key index."""
        now = time.monotonic()
        for _ in range(len(self.clients)):
            idx = next(self._cycle)
            if self._backoff.get(idx, 0) <= now:
                return idx
        raise RuntimeError("All Groq API keys are rate-limited. Add more keys or wait.")

    def _seconds_until_free(self) -> float:
        """Seconds until the first key leaves its cooldown."""
        now = time.monotonic()
        return max(0.0, min(self._backoff.get(i, 0) for i in range(len(self.clients))) - now)

    def _mark_backoff(self, idx: int, seconds: float = 60):
        self._backoff[idx] = time.monotonic() + seconds
        logger.warning(f"Groq key #{idx} backed off for {seconds:.0f}s")

    @staticmethod
    def _rate_limit_backoff(exc) -> float:
        """
        Cooldown for a 429. A per-minute limit clears after the retry-after
        delay (about a minute); a per-day limit (TPD/RPD) does not clear before
        Groq's retry-after, so that key is parked for that long (at least 1 h).
        """
        msg = str(exc).lower()
        retry_after = None
        try:
            ra = exc.response.headers.get("retry-after")
            retry_after = float(ra) if ra else None
        except Exception:
            pass
        if "per day" in msg:
            return max(retry_after or 0, 3600)
        return max(retry_after or 0, 20) + 2

    async def _wait_for_key(self, estimate: int = 0) -> tuple[int, Optional[list]]:
        """
        Return an available key, sleeping while all keys are cooling down.
        Raises only if no key frees up within GROQ_MAX_WAIT_SECONDS (for
        example when every key has reached its daily limit).
        """
        from app.core.config import settings
        max_wait = float(getattr(settings, "GROQ_MAX_WAIT_SECONDS", 900))
        waited = 0.0
        while True:
            async with self._lock:
                idx, delay, entry = self._reserve(estimate)
                if idx is not None:
                    return idx, entry
            if waited + delay > max_wait:
                raise RuntimeError(
                    f"All Groq API keys are rate-limited for at least {delay:.0f}s more "
                    f"(daily limit reached on every key?). Wait or add keys."
                )
            logger.warning("All Groq keys at their per-minute limit or cooling down; waiting %.0fs", delay + 1)
            await asyncio.sleep(delay + 1)
            waited += delay + 1

    async def chat(
        self,
        messages: list[dict],
        model: Optional[str] = None,
        max_tokens: int = 2048,
        temperature: float = 0.1,
        stream: bool = False,
        **kwargs,
    ):
        """Run a chat completion with automatic key rotation on 429 and bad keys."""
        from groq import AuthenticationError, RateLimitError, APITimeoutError, NotFoundError
        model = model or _default_model()

        auth_errors: list[str] = []
        # rough size of this request: ~4 characters per token, plus the answer
        estimate = sum(len(str(m.get("content", ""))) for m in messages) // 4 + min(max_tokens, 1500)
        for attempt in range(len(self.clients) * 4):
            try:
                idx, entry = await self._wait_for_key(estimate)
            except RuntimeError as e:
                if auth_errors:
                    raise RuntimeError(
                        f"All available Groq API keys were rejected or rate-limited. "
                        f"Errors: {'; '.join(auth_errors)}"
                    )
                raise e

            try:
                client = self.clients[idx]
                response = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    stream=stream,
                    **kwargs,
                )
                # replace the estimate with the real count when Groq reports it
                try:
                    if entry is not None and response.usage and response.usage.total_tokens:
                        entry[1] = float(response.usage.total_tokens)
                except Exception:
                    pass
                return response

            except RateLimitError as e:
                self._mark_backoff(idx, self._rate_limit_backoff(e))
                # Next iteration picks the next available key, or waits for one
                continue

            except APITimeoutError:
                self._mark_backoff(idx, 30) # Backoff for 30s on timeout
                logger.warning(f"Groq key #{idx} timed out; trying another configured key")
                continue

            except AuthenticationError as e:
                auth_errors.append(f"key #{idx}: {e}")
                self._mark_backoff(idx, 3600)
                logger.error(f"Groq key #{idx} authentication failed; trying another configured key")
                # Next iteration of the loop will pick the next available key
                continue

            except NotFoundError as e:
                # The model itself does not exist / was deprecated. Rotating keys
                # cannot help — every key returns the same 404 — so fail fast with
                # an actionable message instead of silently killing the grade.
                raise GroqModelUnavailableError(
                    f"Groq model '{model}' is unavailable (404): {e}. "
                    f"Groq has likely deprecated it. Set GROQ_MAIN_MODEL / "
                    f"GROQ_FAST_MODEL in backend/.env to a currently supported "
                    f"model (see https://console.groq.com/docs/models) and restart."
                ) from e

            except Exception as e:
                logger.error(f"Groq key #{idx} error: {e}")
                raise

        if auth_errors:
            raise RuntimeError(
                "All available Groq API keys were rejected. Update GROQ_API_KEYS in backend/.env "
                "with valid keys from https://console.groq.com/keys and restart the backend."
            )

        raise RuntimeError("Exhausted all Groq API key rotation attempts.")

    async def chat_text(self, messages: list[dict], **kwargs) -> str:
        """Convenience: return just the text content."""
        r = await self.chat(messages, **kwargs)
        return r.choices[0].message.content

    async def validate_models(self, models: list[str]) -> dict:
        """
        Pre-flight check: verify each configured model is actually reachable
        before any grade runs, so a deprecated model is caught at startup /
        via the health endpoint instead of silently killing a background grade.

        Returns {"ok": bool, "available": [...], "missing": [...], "error": str|None}.
        Uses the Groq /models listing (no token cost). Never raises.
        """
        from groq import AuthenticationError
        wanted = [m for m in dict.fromkeys(models) if m]
        for idx in range(len(self.clients)):
            try:
                listing = await self.clients[idx].models.list()
                available = {m.id for m in listing.data}
                missing = [m for m in wanted if m not in available]
                return {
                    "ok": not missing,
                    "available": sorted(available),
                    "missing": missing,
                    "error": None,
                }
            except AuthenticationError:
                continue  # try the next key
            except Exception as e:  # network/other — report, don't crash startup
                return {"ok": False, "available": [], "missing": wanted, "error": str(e)}
        return {"ok": False, "available": [], "missing": wanted,
                "error": "All Groq API keys were rejected (authentication failed)."}

    async def stream_chat(self, messages: list[dict], model: Optional[str] = None, **kwargs):
        """Yield text tokens from a streaming completion."""
        from groq import RateLimitError, AuthenticationError, APITimeoutError
        model = model or _default_model()

        estimate = sum(len(str(m.get("content", ""))) for m in messages) // 4 + min(kwargs.get("max_tokens", 1024), 1500)
        for attempt in range(len(self.clients) * 4):
            # same key selection as chat(): waits for a free key and respects
            # the per-minute token throttle instead of failing immediately
            idx, entry = await self._wait_for_key(estimate)
            started = False
            try:
                client = self.clients[idx]
                stream = await client.chat.completions.create(
                    model=model, messages=messages, stream=True,
                    max_tokens=kwargs.get('max_tokens', 1024),
                    temperature=kwargs.get('temperature', 0.2),
                )
                async for chunk in stream:
                    # Some chunks carry no choices (e.g. the final usage-only
                    # chunk); indexing them raised "'NoneType' object is not
                    # subscriptable" and aborted the answer.
                    choices = getattr(chunk, "choices", None) or []
                    if not choices:
                        continue
                    delta = getattr(choices[0], "delta", None)
                    content = getattr(delta, "content", None) if delta else None
                    if content:
                        started = True
                        yield content
                return  # Success

            except (RateLimitError, AuthenticationError, APITimeoutError) as e:
                if started:
                    raise  # part of the answer was already sent; do not repeat it
                if isinstance(e, RateLimitError):
                    backoff = self._rate_limit_backoff(e)
                elif isinstance(e, AuthenticationError):
                    backoff = 3600
                else:
                    backoff = 30
                self._mark_backoff(idx, backoff)
                logger.warning(f"Streaming error on key #{idx}: {e}. Retrying with next key...")
                continue
            except Exception as e:
                logger.error(f"Fatal streaming error on key #{idx}: {e}")
                raise

        raise RuntimeError("All Groq keys failed during streaming.")


# ── Singleton factory ──────────────────────────────────────────────────────────
_client: Optional[GroqRotationClient] = None

def get_groq_client() -> GroqRotationClient:
    global _client
    if _client is None:
        from app.core.config import settings
        keys = [k.strip() for k in settings.GROQ_API_KEYS.split(",") if k.strip()]
        _client = GroqRotationClient(keys)
        logger.info(f"Groq rotation client initialized with {len(keys)} key(s)")
    return _client