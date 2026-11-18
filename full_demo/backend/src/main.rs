mod telemetry;

use std::{collections::HashMap, time::Duration};

use axum::{http::Request, routing::get};
use opentelemetry::{KeyValue, global, metrics::Counter};
use serde::{Deserialize, Serialize};
use serde_json::json;
use socketioxide::{
    SocketIo,
    extract::{AckSender, SocketRef, TryData},
};
use tower_http::trace::TraceLayer;
use tracing::{Instrument, info, info_span, warn};
use tracing_opentelemetry::OpenTelemetrySpanExt;

type Carrier = HashMap<String, String>;

fn required_env(name: &str) -> Result<String, String> {
    std::env::var(name).map_err(|_| format!("{name} is not set (expected in ../.env or the environment)"))
}

fn emit_traced<T: Serialize>(s: &SocketRef, event: &str, data: &T) {
    let mut carrier = Carrier::new();
    let cx = tracing::Span::current().context();
    global::get_text_map_propagator(|p| p.inject_context(&cx, &mut carrier));
    if let Err(err) = s.emit(event, &(data, carrier)) {
        warn!(%err, event, "socket.io emit failed");
    }
}

fn receive_span(s: &SocketRef, event: &str, carrier: Option<&Carrier>) -> tracing::Span {
    let span = info_span!(
        "socketio receive",
        otel.name = %format!("socketio receive {event}"),
        otel.kind = "server",
        socket.id = %s.id,
        messaging.system = "socket.io",
        messaging.destination.name = event,
        messaging.operation.type = "receive",
    );
    match carrier {
        Some(carrier) => {
            let parent = global::get_text_map_propagator(|p| p.extract(carrier));
            let _ = span.set_parent(parent);
        }
        None => warn!(event, "event without (data, carrier) arguments; trace not linked"),
    }
    span
}

#[derive(Clone)]
struct Controller {
    http: reqwest::Client,
    send_url: String,
}

#[derive(Deserialize)]
struct SendResponse {
    response: String,
}

impl Controller {
    fn new(host: &str, port: u16) -> Result<Self, reqwest::Error> {
        Ok(Self {
            http: reqwest::Client::builder().timeout(Duration::from_secs(5)).build()?,
            send_url: format!("http://{host}:{port}/send"),
        })
    }

    async fn send(&self, payload: &str) -> Result<String, String> {
        let span = info_span!(
            "controller send",
            otel.name = "POST /send",
            otel.kind = "client",
            http.request.method = "POST",
            url.full = %self.send_url,
            scpi.command = payload,
            http.response.status_code = tracing::field::Empty,
            otel.status_code = tracing::field::Empty,
        );
        async {
            let mut headers = reqwest::header::HeaderMap::new();
            let cx = tracing::Span::current().context();
            global::get_text_map_propagator(|p| {
                p.inject_context(&cx, &mut opentelemetry_http::HeaderInjector(&mut headers))
            });
            let result = async {
                let resp = self
                    .http
                    .post(&self.send_url)
                    .query(&[("payload", payload)])
                    .headers(headers)
                    .send()
                    .await
                    .map_err(|e| format!("controller request failed: {e}"))?;
                let status = resp.status();
                tracing::Span::current().record("http.response.status_code", status.as_u16());
                if !status.is_success() {
                    return Err(format!("controller returned {status}"));
                }
                let body: SendResponse = resp
                    .json()
                    .await
                    .map_err(|e| format!("bad controller response: {e}"))?;
                Ok(body.response)
            }
            .await;
            if let Err(err) = &result {
                tracing::Span::current().record("otel.status_code", "ERROR");
                warn!(%err, "controller send failed");
            }
            result
        }
        .instrument(span)
        .await
    }

    async fn set(&self, header: &str, value: f64) -> Result<(), String> {
        if !value.is_finite() {
            return Err(format!("value must be a finite number, got {value}"));
        }
        let reply = self.send(&format!("*CLS;{header} {value};:SYST:ERR?")).await?;
        if reply.starts_with("0,") {
            Ok(())
        } else {
            Err(format!("supply rejected {header} {value}: {reply}"))
        }
    }
}

#[derive(Debug, Serialize)]
struct SupplyStatus {
    voltage: f64,
    current: f64,
    power: f64,
    voltage_setpoint: f64,
    current_limit: f64,
    ovp_level: f64,
    ovp_tripped: bool,
    output_enabled: bool,
    mode: String,
}

impl SupplyStatus {
    const QUERY: &'static str = "MEAS:VOLT?;:MEAS:CURR?;:MEAS:POW?;:VOLT?;:CURR?;\
                                 :VOLT:PROT?;:VOLT:PROT:TRIP?;:OUTP?;:OUTP:MODE?";

    fn parse(reply: &str) -> Result<Self, String> {
        let fields: Vec<&str> = reply.split(';').map(str::trim).collect();
        let [v, i, p, vset, ilim, ovp, tripped, out, mode] = fields[..] else {
            return Err(format!("expected 9 fields in status reply, got {}: {reply:?}", fields.len()));
        };
        let num = |name: &str, s: &str| {
            s.parse::<f64>().map_err(|e| format!("bad {name} {s:?} in status reply: {e}"))
        };
        Ok(Self {
            voltage: num("voltage", v)?,
            current: num("current", i)?,
            power: num("power", p)?,
            voltage_setpoint: num("voltage setpoint", vset)?,
            current_limit: num("current limit", ilim)?,
            ovp_level: num("OVP level", ovp)?,
            ovp_tripped: tripped == "1",
            output_enabled: out == "1",
            mode: mode.to_owned(),
        })
    }
}

async fn poll_status(io: SocketIo, controller: Controller, period: Duration) {
    let meter = global::meter("backend");
    let gauge = |name: &'static str, unit: &'static str, description: &'static str| {
        meter.f64_gauge(name).with_unit(unit).with_description(description).build()
    };
    let voltage = gauge("power_supply.voltage", "V", "Measured output voltage");
    let current = gauge("power_supply.current", "A", "Measured output current");
    let power = gauge("power_supply.power", "W", "Measured output power");
    let voltage_setpoint = gauge("power_supply.voltage.setpoint", "V", "Programmed voltage");
    let current_limit = gauge("power_supply.current.limit", "A", "Programmed current limit");
    let ovp_level = gauge("power_supply.voltage.ovp_level", "V", "Over-voltage protection level");
    let poll_failures = meter
        .u64_counter("power_supply.status.poll_failures")
        .with_description("Status polls that failed")
        .build();

    let mut ticker = tokio::time::interval(period);
    // If a poll overruns (e.g. the controller is slow), don't fire a burst to catch up.
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    loop {
        ticker.tick().await;
        let span = info_span!("poll status", otel.status_code = tracing::field::Empty);
        async {
            let status = controller
                .send(SupplyStatus::QUERY)
                .await
                .and_then(|reply| SupplyStatus::parse(&reply));
            match status {
                Ok(status) => {
                    voltage.record(status.voltage, &[]);
                    current.record(status.current, &[]);
                    power.record(status.power, &[]);
                    voltage_setpoint.record(status.voltage_setpoint, &[]);
                    current_limit.record(status.current_limit, &[]);
                    ovp_level.record(status.ovp_level, &[]);

                    let mut carrier = Carrier::new();
                    let cx = tracing::Span::current().context();
                    global::get_text_map_propagator(|p| p.inject_context(&cx, &mut carrier));
                    if let Err(err) = io.emit("status", &(&status, carrier)).await {
                        warn!(%err, "status broadcast failed");
                    }
                }
                Err(err) => {
                    tracing::Span::current().record("otel.status_code", "ERROR");
                    poll_failures.add(1, &[]);
                    warn!(%err, "status poll failed");
                }
            }
        }
        .instrument(span)
        .await;
    }
}

#[derive(Deserialize)]
struct SetPoint {
    value: f64,
}

fn on_setpoint(
    s: &SocketRef,
    event: &'static str,
    scpi_header: &'static str,
    controller: Controller,
    events: Counter<u64>,
) {
    s.on(
        event,
        async move |s: SocketRef,
                    TryData(args): TryData<(serde_json::Value, Carrier)>,
                    ack: AckSender| {
            events.add(1, &[KeyValue::new("event", event)]);
            let (data, carrier) = match args {
                Ok((data, carrier)) => (data, Some(carrier)),
                Err(_) => (serde_json::Value::Null, None),
            };
            let span = receive_span(&s, event, carrier.as_ref());
            async move {
                let result = match serde_json::from_value::<SetPoint>(data) {
                    Ok(SetPoint { value }) => controller.set(scpi_header, value).await,
                    Err(err) => Err(format!("expected {{\"value\": <number>}}: {err}")),
                };
                let reply = match result {
                    Ok(()) => {
                        info!(event, "setpoint applied");
                        json!({ "ok": true })
                    }
                    Err(error) => {
                        warn!(event, %error, "setpoint failed");
                        json!({ "ok": false, "error": error })
                    }
                };
                ack.send(&reply).ok();
            }
            .instrument(span)
            .await;
        },
    );
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    dotenvy::dotenv().ok();

    let _telemetry = telemetry::init("backend")?;

    let backend_host = required_env("backend_host")?;
    let backend_port: u16 = required_env("backend_port")?.parse()?;
    let controller = Controller::new(
        &required_env("backend_power_supply_controller_host")?,
        required_env("backend_power_supply_controller_port")?.parse()?,
    )?;

    let meter = global::meter("backend");
    let connections = meter
        .i64_up_down_counter("socketio.connections.active")
        .with_description("Open socket.io connections")
        .build();
    let events = meter
        .u64_counter("socketio.events")
        .with_description("socket.io events received")
        .build();

    let (layer, io) = SocketIo::new_layer();

    tokio::spawn(poll_status(io.clone(), controller.clone(), Duration::from_millis(100)));

    io.ns("/", async move |s: SocketRef| {
        info!(socket.id = %s.id, "socket connected");
        connections.add(1, &[]);
        s.on_disconnect(async move |s: SocketRef| {
            info!(socket.id = %s.id, "socket disconnected");
            connections.add(-1, &[]);
        });

        on_setpoint(&s, "setVoltage", "VOLT", controller.clone(), events.clone());
        on_setpoint(&s, "setCurrent", "CURR", controller.clone(), events.clone());
        on_setpoint(&s, "setOutput", "OUTP", controller.clone(), events.clone());

        s.on(
            "message",
            async move |s: SocketRef,
                        TryData(args): TryData<(serde_json::Value, Carrier)>,
                        ack: AckSender| {
                events.add(1, &[KeyValue::new("event", "message")]);
                let span = info_span!(
                    "socketio receive message",
                    otel.kind = "server",
                    socket.id = %s.id,
                    messaging.system = "socket.io",
                    messaging.destination.name = "message",
                    messaging.operation.type = "receive",
                );
                let data = match args {
                    Ok((data, carrier)) => {
                        let parent = global::get_text_map_propagator(|p| p.extract(&carrier));
                        let _ = span.set_parent(parent);
                        data
                    }
                    Err(err) => {
                        warn!(%err, "message without (data, carrier) arguments; trace not linked");
                        serde_json::Value::Null
                    }
                };
                async move {
                    info!(%data, "replying to message");
                    ack.send(&"Hello World!").ok();
                    emit_traced(&s, "message-back", &"Hello World!");
                }
                .instrument(span)
                .await;
            },
        );
    });

    let app = axum::Router::new()
        .route("/", get(async || "Hello, World!"))
        .layer(layer)
        .layer(TraceLayer::new_for_http().make_span_with(|req: &Request<_>| {
            let span = info_span!(
                "http request",
                otel.name = %format!("{} {}", req.method(), req.uri().path()),
                otel.kind = "server",
                http.request.method = %req.method(),
                url.path = %req.uri().path(),
            );
            let parent = global::get_text_map_propagator(|p| {
                p.extract(&opentelemetry_http::HeaderExtractor(req.headers()))
            });
            let _ = span.set_parent(parent);
            span
        }));

    let listener = tokio::net::TcpListener::bind(format!("{backend_host}:{backend_port}"))
        .await
        .unwrap();
    info!("listening on {backend_host}:{backend_port}");
    axum::serve(listener, app).await.unwrap();

    Ok(())
}
