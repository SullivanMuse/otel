"""OpenTelemetry setup: traces, metrics and logs exported over OTLP/HTTP.

Configured with the standard OTEL_* environment variables, e.g.
    OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318   (default)
    OTEL_SERVICE_NAME=...                               (overrides the default name)
    OTEL_SDK_DISABLED=true                              (turn it all off)
"""

import logging
import os

from opentelemetry import _logs, metrics, trace
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_configured = False


def setup_telemetry(service_name: str, log_level: int = logging.INFO) -> None:
    """Install global tracer/meter/logger providers. Safe to call more than once."""
    global _configured
    if _configured:
        return
    _configured = True

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s [trace=%(otelTraceID)s] %(message)s",
    )
    for handler in logging.getLogger().handlers:
        handler.addFilter(_TraceIdFilter())

    if os.environ.get("OTEL_SDK_DISABLED", "").lower() == "true":
        return

    resource = Resource.create(
        {"service.name": os.environ.get("OTEL_SERVICE_NAME", service_name)}
    )

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    metrics.set_meter_provider(
        MeterProvider(
            resource=resource,
            metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter())],
        )
    )

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    _logs.set_logger_provider(logger_provider)
    logging.getLogger().addHandler(LoggingHandler(logger_provider=logger_provider))


class _TraceIdFilter(logging.Filter):
    """Adds otelTraceID to every record so console logs can be correlated with traces."""

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = trace.get_current_span().get_span_context()
        record.otelTraceID = format(ctx.trace_id, "032x") if ctx.is_valid else "-"
        return True
