#![allow(unexpected_cfgs)]
error_chain! {
    types {
        Error, ErrorKind, ResultExt, Result;
    }

    errors {
        Connection(msg: String) {
            description("Connection error")
            display("Connection error: {}", msg)
        }

        Interrupt(sig: i32) {
            description("Interruption by external signal")
            display("Iterrupted by signal {}", sig)
        }

        // The daemon answered a JSON-RPC call with an error object. The text is the
        // same as when this was a plain message; REST and Electrum clients see it.
        RpcError(method: String, code: i64, error: serde_json::Value) {
            description("RPC error")
            display("{} RPC error: {}", method, error)
        }

        TooManyUtxos(limit: usize) {
            description("Too many unspent transaction outputs. Contact support to raise limits.")
            display("Too many unspent transaction outputs (>{}). Contact support to raise limits.", limit)
        }

        TooManyTxs(limit: usize) {
            description("Too many history transactions. Contact support to raise limits.")
            display("Too many history transactions (>{}). Contact support to raise limits.", limit)
        }

        #[cfg(feature = "electrum-discovery")]
        ElectrumClient(e: electrum_client::Error) {
            description("Electrum client error")
            display("Electrum client error: {:?}", e)
        }

    }
}

#[cfg(feature = "electrum-discovery")]
impl From<electrum_client::Error> for Error {
    fn from(e: electrum_client::Error) -> Self {
        Error::from(ErrorKind::ElectrumClient(e))
    }
}
