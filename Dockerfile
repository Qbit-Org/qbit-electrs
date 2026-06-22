FROM debian:bookworm-slim AS base

ENV CARGO_NET_GIT_FETCH_WITH_CLI=true

RUN apt update -qy && \
    apt install -qy librocksdb-dev curl

FROM base AS build

RUN apt install -qy git clang cmake

ENV RUSTUP_HOME=/rust
ENV CARGO_HOME=/cargo
ENV PATH=/cargo/bin:/rust/bin:$PATH

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y

WORKDIR /build
COPY . .

RUN cargo build --release --bin electrs

FROM base AS deploy

COPY --from=build /build/target/release/electrs /bin/electrs

EXPOSE 3000 3002 3004 4224 24224 44224 50001 40001 60401

ENTRYPOINT ["/bin/electrs"]
