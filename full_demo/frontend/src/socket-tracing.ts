import {
  context,
  propagation,
  SpanKind,
  SpanStatusCode,
  type Context,
} from '@opentelemetry/api'
import type { Socket } from 'socket.io-client'
import { tracer } from './telemetry'

type Carrier = Record<string, string>

const baseAttributes = (event: string) => ({
  'messaging.system': 'socket.io',
  'messaging.destination.name': event,
})

function injectCarrier(ctx: Context): Carrier {
  const carrier: Carrier = {}
  propagation.inject(ctx, carrier)
  console.log("carrier", carrier);
  return carrier
}

function isCarrier(value: unknown): value is Carrier {
  return typeof value === 'object' && value !== null && 'traceparent' in value
}

export function tracedEmit<T = unknown>(
  socket: Socket,
  event: string,
  data: unknown = null,
  timeoutMs = 5000,
): Promise<T> {
  return tracer.startActiveSpan(
    `socketio emit ${event}`,
    {
      kind: SpanKind.CLIENT,
      attributes: {
        ...baseAttributes(event),
        'messaging.operation.type': 'send',
        'messaging.operation.name': 'emit',
      },
    },
    (span) =>
      new Promise<T>((resolve, reject) => {
        const carrier = injectCarrier(context.active())
        socket.timeout(timeoutMs).emit(event, data, carrier, (err: Error | null, reply: T) => {
          if (err) {
            span.recordException(err)
            span.setStatus({ code: SpanStatusCode.ERROR, message: err.message })
            reject(err)
          } else {
            resolve(reply)
          }
          span.end()
        })
      }),
  )
}

export function traceIncomingEvents(socket: Socket): void {
  socket.onAny((event: string, ...args: unknown[]) => {
    const last = args.at(-1)
    const parent = isCarrier(last)
      ? propagation.extract(context.active(), last)
      : context.active()
    const span = tracer.startSpan(
      `socketio receive ${event}`,
      {
        kind: SpanKind.CONSUMER,
        attributes: {
          ...baseAttributes(event),
          'messaging.operation.type': 'receive',
          'messaging.operation.name': 'receive',
        },
      },
      parent,
    )
    span.end()
  })
}
