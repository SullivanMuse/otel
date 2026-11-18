import { context, propagation, trace } from '@opentelemetry/api'
import { ZoneContextManager } from '@opentelemetry/context-zone'
import {
  CompositePropagator,
  W3CBaggagePropagator,
  W3CTraceContextPropagator,
} from '@opentelemetry/core'
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-http'
import { registerInstrumentations } from '@opentelemetry/instrumentation'
import { DocumentLoadInstrumentation } from '@opentelemetry/instrumentation-document-load'
import { FetchInstrumentation } from '@opentelemetry/instrumentation-fetch'
import { XMLHttpRequestInstrumentation } from '@opentelemetry/instrumentation-xml-http-request'
import { resourceFromAttributes } from '@opentelemetry/resources'
import { BatchSpanProcessor, TracerProvider } from '@opentelemetry/sdk-trace'
import { ATTR_SERVICE_NAME, ATTR_SERVICE_VERSION } from '@opentelemetry/semantic-conventions'

const endpoint = __OTLP_TRACES_ENDPOINT__

const provider = new TracerProvider({
  resource: resourceFromAttributes({
    [ATTR_SERVICE_NAME]: 'frontend',
    [ATTR_SERVICE_VERSION]: '0.0.0',
  }),
  // Flushes automatically when the page is hidden/closed.
  spanProcessors: [new BatchSpanProcessor({ exporter: new OTLPTraceExporter({ url: endpoint }) })],
})

trace.setGlobalTracerProvider(provider)
context.setGlobalContextManager(new ZoneContextManager().enable())
propagation.setGlobalPropagator(
  new CompositePropagator({
    propagators: [new W3CTraceContextPropagator(), new W3CBaggagePropagator()],
  }),
)

// socket.io's long-polling transport would otherwise produce a span per poll.
const ignoreUrls = [/\/socket\.io\//, endpoint]

registerInstrumentations({
  instrumentations: [
    new DocumentLoadInstrumentation(),
    new FetchInstrumentation({ ignoreUrls }),
    new XMLHttpRequestInstrumentation({ ignoreUrls }),
  ],
})

export const tracer = trace.getTracer('frontend')
