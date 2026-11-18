import asyncio
import logging
import time

import pydantic_settings
from opentelemetry import metrics, propagate, trace
from opentelemetry.context import Context
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.trace import SpanKind, Status, StatusCode
from supply import NoiseModel, PowerSupply, SupplyLimits
from telemetry import setup_telemetry

logger = logging.getLogger("power_supply_sim")
tracer = trace.get_tracer("power_supply_sim")
meter = metrics.get_meter("power_supply_sim")

commands_counter = meter.create_counter(
    "scpi.commands", unit="{line}", description="SCPI lines processed"
)
errors_counter = meter.create_counter(
    "scpi.errors", unit="{error}", description="SCPI errors pushed to the error queue"
)
command_duration = meter.create_histogram(
    "scpi.command.duration", unit="s", description="Time to process one SCPI line"
)
active_connections = meter.create_up_down_counter(
    "scpi.connections.active", unit="{connection}", description="Open client connections"
)


class Config(pydantic_settings.BaseSettings):
    host: str = "127.0.0.1"
    port: int = 8001

    max_voltage: float = 30.0
    max_current: float = 5.0
    load_resistance: float = 10.0

    voltage_noise_gain: float = 0.0002
    voltage_noise_offset: float = 0.0005
    current_noise_gain: float = 0.0005
    current_noise_offset: float = 0.0002

    seed: int | None = None

    model_config = pydantic_settings.SettingsConfigDict(
        env_prefix="power_supply_sim_",
        env_file="../.env",
        extra="ignore",
    )


def make_supply(config: Config) -> PowerSupply:
    return PowerSupply(
        limits=SupplyLimits(
            max_voltage=config.max_voltage,
            max_current=config.max_current,
            max_ovp=config.max_voltage * 1.1,
        ),
        noise=NoiseModel(
            voltage_gain=config.voltage_noise_gain,
            voltage_offset=config.voltage_noise_offset,
            current_gain=config.current_noise_gain,
            current_offset=config.current_noise_offset,
        ),
        load_resistance=config.load_resistance,
        seed=config.seed,
    )


def register_state_gauges(supply: PowerSupply) -> None:
    """Expose the instrument's state as observable gauges, read on each metric export."""

    def gauge(name: str, unit: str, description: str, read):
        def callback(_options: CallbackOptions):
            yield Observation(float(read()))

        meter.create_observable_gauge(name, callbacks=[callback], unit=unit, description=description)

    gauge("psu.voltage.setpoint", "V", "Programmed voltage", lambda: supply.voltage_setpoint)
    gauge("psu.current.limit", "A", "Programmed current limit", lambda: supply.current_limit)
    gauge("psu.output.enabled", "1", "1 if the output is on", lambda: supply.output_enabled)
    gauge("psu.ovp.tripped", "1", "1 if over-voltage protection tripped", lambda: supply.ovp_tripped)
    gauge("psu.load.resistance", "Ohm", "Simulated load resistance", lambda: supply.load_resistance)
    gauge("psu.output.voltage", "V", "Ideal output voltage", lambda: supply._ideal_output()[0])
    gauge("psu.output.current", "A", "Ideal output current", lambda: supply._ideal_output()[1])


_CARRIER_KEYS = ("traceparent", "tracestate", "baggage")


def split_trace_context(line: str) -> tuple[Context, str]:
    """Strip leading `traceparent=... tracestate=... baggage=...` tokens from a line.

    Returns the extracted parent context and the remaining SCPI text. SCPI headers never
    contain '=', so plain SCPI lines (no prefix) pass through unchanged with an empty context.
    """
    carrier: dict[str, str] = {}
    rest = line
    while True:
        token, _, remainder = rest.partition(" ")
        key, sep, value = token.partition("=")
        if not sep or key not in _CARRIER_KEYS:
            break
        carrier[key] = value
        rest = remainder.lstrip()
    return propagate.extract(carrier), rest


def process_line(supply: PowerSupply, line: str, peer: str) -> str | None:
    """Run one SCPI line through the supply inside a span, recording metrics."""
    parent, line = split_trace_context(line)
    errors_before = len(supply.errors)
    start = time.perf_counter()
    with tracer.start_as_current_span(
        "scpi.command",
        context=parent,
        kind=SpanKind.SERVER,
        attributes={"scpi.command": line, "client.address": peer},
    ) as span:
        response = supply.handle_line(line)
        if response is not None:
            span.set_attribute("scpi.response", response)
        new_errors = [str(e) for e in list(supply.errors)[errors_before:]]
        if new_errors:
            span.set_attribute("scpi.errors", new_errors)
            span.set_status(Status(StatusCode.ERROR, new_errors[0]))
            errors_counter.add(len(new_errors))
            logger.warning("SCPI error for %r: %s", line, "; ".join(new_errors))
        logger.debug("%s %r -> %r", peer, line, response)
    outcome = "error" if new_errors else "ok"
    commands_counter.add(1, {"outcome": outcome})
    command_duration.record(time.perf_counter() - start, {"outcome": outcome})
    return response


async def handle_client(
    supply: PowerSupply, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
):
    addr = writer.get_extra_info("peername")
    peer = f"{addr[0]}:{addr[1]}" if addr else "unknown"
    logger.info("connected: %s", peer)
    active_connections.add(1)
    try:
        while True:
            data = await reader.readline()
            if not data:  # EOF
                break
            line = data.decode(errors="replace").strip()
            response = process_line(supply, line, peer)
            if response is not None:
                writer.write(response.encode() + b"\n")
                await writer.drain()
    except (ConnectionResetError, asyncio.IncompleteReadError):
        pass
    finally:
        active_connections.add(-1)
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionResetError:
            pass
        logger.info("disconnected: %s", peer)


async def run_server(config: Config):
    supply = make_supply(config)
    register_state_gauges(supply)
    server = await asyncio.start_server(
        lambda r, w: handle_client(supply, r, w), config.host, config.port
    )
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
    logger.info("%s listening on %s", supply.IDN, addrs)
    async with server:
        await server.serve_forever()


def main():
    setup_telemetry("power-supply-sim")
    asyncio.run(run_server(Config()))


if __name__ == "__main__":
    main()
