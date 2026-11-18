import asyncio
import logging
import pprint
from contextlib import asynccontextmanager
import sys

import pydantic_settings
import uvicorn
from fastapi import FastAPI, Request
from opentelemetry import metrics, propagate, trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.trace import SpanKind
from telemetry import setup_telemetry

setup_telemetry("power-supply-controller")
logger = logging.getLogger("power_supply_controller")
tracer = trace.get_tracer("power_supply_controller")
meter = metrics.get_meter("power_supply_controller")
scpi_exchange_duration = meter.create_histogram(
    "scpi.exchange.duration",
    unit="s",
    description="Round trip of one SCPI line to the supply, including lock wait",
)


class Config(pydantic_settings.BaseSettings):
    host: str = "127.0.0.1"
    port: int = 8001

    supply_host: str = "127.0.0.1"
    supply_port: int = 8002

    model_config = pydantic_settings.SettingsConfigDict(
        env_prefix="power_supply_controller_",
        env_file="../.env",
        extra="ignore",
    )


async def run_client(host: str, port: int):
    reader, writer = await asyncio.open_connection(host, port)
    loop = asyncio.get_running_loop()
    print("[client] connected. Type a message (empty line or Ctrl-D to quit).")
    try:
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line.strip():
                break
            writer.write(line.encode())
            await writer.drain()
            reply = await reader.readline()
            if not reply:
                print("[client] server closed the connection")
                break
            print(f"[client] echo: {reply.decode().rstrip()}")
    finally:
        writer.close()
        await writer.wait_closed()


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = Config()
    reader, writer = await asyncio.open_connection(config.supply_host, config.supply_port)
    logger.info("connected to supply at %s:%s", config.supply_host, config.supply_port)
    app.state.config = config
    app.state.tcp_reader = reader
    app.state.tcp_writer = writer
    app.state.tcp_lock = asyncio.Lock()

    try:
        yield
    except Exception:
        writer.close()
        await writer.wait_closed()
        raise


app = FastAPI(lifespan=lifespan)
FastAPIInstrumentor.instrument_app(app)


@app.post("/send")
async def send(request: Request, payload: str):
    state = request.app.state
    config = state.config
    start = asyncio.get_running_loop().time()
    with tracer.start_as_current_span(
        "scpi.exchange",
        kind=SpanKind.CLIENT,
        attributes={
            "scpi.command": payload,
            "server.address": config.supply_host,
            "server.port": config.supply_port,
        },
    ) as span:
        carrier: dict[str, str] = {}
        propagate.inject(carrier)
        prefix = "".join(f"{k}={v} " for k, v in carrier.items())
        async with state.tcp_lock:
            span.add_event("lock acquired")
            state.tcp_writer.write((prefix + payload).encode() + b"\n")
            await state.tcp_writer.drain()
            line = await state.tcp_reader.readline()
        response = line.decode().strip()
        span.set_attribute("scpi.response", response)
    scpi_exchange_duration.record(asyncio.get_running_loop().time() - start)
    return {"response": response}


if __name__ == "__main__":
    config = Config()
    pprint.pprint({"config": config.model_dump(mode="python")})
    uvicorn.run("main:app", host=config.host, port=config.port, reload=True)
