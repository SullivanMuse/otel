FROM rust:1-alpine3.24 as builder
WORKDIR /usr/local/cargo/bin/
COPY . .
RUN cargo install --path .

FROM debian:bullseye-slim
# RUN apt-get update && apt-get install -y extra-runtime-dependencies && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=builder /usr/local/cargo/bin/backend /app
CMD ["./backend"]
