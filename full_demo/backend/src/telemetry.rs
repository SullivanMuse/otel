use opentelemetry::propagation::TextMapCompositePropagator;
use opentelemetry::{KeyValue, global, trace::TracerProvider as _};
use opentelemetry_appender_tracing::layer::OpenTelemetryTracingBridge;
use opentelemetry_sdk::{
    Resource,
    logs::SdkLoggerProvider,
    metrics::SdkMeterProvider,
    propagation::{BaggagePropagator, TraceContextPropagator},
    trace::SdkTracerProvider,
};
use tracing_subscriber::{EnvFilter, Layer as _, layer::SubscriberExt, util::SubscriberInitExt};

pub struct TelemetryGuard {
    tracer_provider: SdkTracerProvider,
    meter_provider: SdkMeterProvider,
    logger_provider: SdkLoggerProvider,
}

impl Drop for TelemetryGuard {
    fn drop(&mut self) {
        for result in [
            self.tracer_provider.shutdown(),
            self.meter_provider.shutdown(),
            self.logger_provider.shutdown(),
        ] {
            if let Err(err) = result {
                eprintln!("telemetry shutdown error: {err}");
            }
        }
    }
}

pub fn init(default_service_name: &'static str) -> Result<TelemetryGuard, Box<dyn std::error::Error>> {
    let mut resource = Resource::builder();
    if std::env::var_os("OTEL_SERVICE_NAME").is_none() {
        resource = resource.with_service_name(default_service_name);
    }
    let resource = resource
        .with_attribute(KeyValue::new("service.version", env!("CARGO_PKG_VERSION")))
        .build();

    let tracer_provider = SdkTracerProvider::builder()
        .with_resource(resource.clone())
        .with_batch_exporter(opentelemetry_otlp::SpanExporter::builder().with_http().build()?)
        .build();

    let meter_provider = SdkMeterProvider::builder()
        .with_resource(resource.clone())
        .with_periodic_exporter(opentelemetry_otlp::MetricExporter::builder().with_http().build()?)
        .build();

    let logger_provider = SdkLoggerProvider::builder()
        .with_resource(resource)
        .with_batch_exporter(opentelemetry_otlp::LogExporter::builder().with_http().build()?)
        .build();

    global::set_tracer_provider(tracer_provider.clone());
    global::set_meter_provider(meter_provider.clone());
    global::set_text_map_propagator(TextMapCompositePropagator::new(vec![
        Box::new(TraceContextPropagator::new()),
        Box::new(BaggagePropagator::new()),
    ]));

    let filter = || {
        EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"))
            // The exporters' own HTTP client must not feed back into the log pipeline.
            .add_directive("hyper=off".parse().unwrap())
            .add_directive("reqwest=off".parse().unwrap())
            .add_directive("opentelemetry=warn".parse().unwrap())
    };

    tracing_subscriber::registry()
        .with(tracing_subscriber::fmt::layer().with_filter(filter()))
        .with(
            tracing_opentelemetry::layer()
                .with_tracer(tracer_provider.tracer(default_service_name))
                .with_filter(filter()),
        )
        .with(OpenTelemetryTracingBridge::new(&logger_provider).with_filter(filter()))
        .try_init()?;

    Ok(TelemetryGuard { tracer_provider, meter_provider, logger_provider })
}
