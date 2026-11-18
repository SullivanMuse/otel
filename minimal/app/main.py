import logging
import time

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from rich.logging import RichHandler

# region traces
processor = BatchSpanProcessor(OTLPSpanExporter())
provider = TracerProvider()
provider.add_span_processor(processor)
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("my.tracer.name")
# endregion

# region metrics
metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter())
provider = MeterProvider(metric_readers=[metric_reader])
metrics.set_meter_provider(provider)
meter = metrics.get_meter("my.meter.name")
loop_counter = meter.create_counter("do_work.counter", unit="1", description="count loop")
# endregion

# region logs
provider = LoggerProvider()
processor = BatchLogRecordProcessor(OTLPLogExporter())
provider.add_log_record_processor(processor)
set_logger_provider(provider)
handler = LoggingHandler(level=logging.INFO, logger_provider=provider)
logging.basicConfig(handlers=[handler, RichHandler()], level=logging.INFO)
logger = logging.getLogger(__name__)
# endregion


@tracer.start_as_current_span("do_work")
def do_work():
    with tracer.start_as_current_span("parent"):
        logger.info("work")
        with tracer.start_as_current_span("child") as child:
            logger.info("nested work")
            try:
                raise ValueError
            except Exception as ex:
                child.set_status(trace.Status(trace.StatusCode.ERROR))
                child.record_exception(ex)
    loop_counter.add(1)
    logger.info("loop")


def main():
    logger.info("Hello from app!")
    while True:
        do_work()
        time.sleep(1)


if __name__ == "__main__":
    main()
