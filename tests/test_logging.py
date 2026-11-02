import io
import logging

from crypto_rag.config import RedactingFormatter


def _capture() -> tuple[logging.Logger, io.StringIO]:
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(RedactingFormatter("%(message)s"))
    log = logging.getLogger("redaction-test")
    log.handlers = [handler]
    log.propagate = False
    return log, buf


def test_api_key_is_masked_in_messages_and_tracebacks():
    log, buf = _capture()
    url = "https://content.guardianapis.com/search?api-key=abc123SECRET&tag=technology/bitcoin"
    log.warning("Retrying: Client error '429' for url '%s'", url)
    try:
        raise RuntimeError(f"failed for {url}")
    except RuntimeError:
        log.exception("request failed")
    out = buf.getvalue()
    assert "abc123SECRET" not in out
    assert out.count("api-key=***") == 2  # the warning and the traceback exception line
    assert "tag=technology/bitcoin" in out
