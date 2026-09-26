import asyncio
import contextlib
import json
import logging
import signal

from aiohttp import web
from telegram import Update
from telegram.ext import Application

logger = logging.getLogger(__name__)

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


def run(application: Application, port: int, webhook_secret: str | None, url_path: str) -> None:
    """Runs `application` behind a small HTTP server, forever.

    Deliberately does NOT call `bot.set_webhook()` - it doesn't know its own
    public URL (that's only known once a host like Cloud Run has assigned
    one). Registering the webhook with Telegram is a separate, explicit step
    performed once the URL is known (see README.md).
    """
    asyncio.run(_serve(application, port, webhook_secret, url_path))


async def _serve(
    application: Application, port: int, webhook_secret: str | None, url_path: str
) -> None:
    await application.initialize()
    if application.post_init:
        await application.post_init(application)
    await application.start()

    async def handle_update(request: web.Request) -> web.Response:
        if webhook_secret and request.headers.get(SECRET_HEADER) != webhook_secret:
            return web.Response(status=401, text="invalid secret token")
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.Response(status=400, text="invalid JSON")
        update = Update.de_json(data, application.bot)
        await application.update_queue.put(update)
        return web.Response(status=200)

    async def health(request: web.Request) -> web.Response:
        return web.Response(status=200, text="ok")

    aio_app = web.Application()
    aio_app.router.add_post(f"/{url_path}", handle_update)
    aio_app.router.add_get("/healthz", health)

    runner = web.AppRunner(aio_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Webhook server listening on 0.0.0.0:%s (path /%s)", port, url_path)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    await stop_event.wait()
    logger.info("Shutting down webhook server")

    await runner.cleanup()
    await application.stop()
    await application.shutdown()
