use jsonschema_valid::{schemas, Config};
use serde_json::Value;
use std::process::Command;

const QBIT_COMMIT: &str = "57bb53575f0d4931e77ac4a34b7e7f4c049f0636";
const LEGACY_P2MR_QBIT_COMMIT: &str = "cdd063fa266401c896b50f566060ce02e15804f7";

fn assert_manifest_valid(schema: &Value, manifest: &Value, manifest_name: &str) {
    let config = Config::from_schema(schema, Some(schemas::Draft::Draft7))
        .expect("manifest schema should compile");
    if let Err(errors) = config.validate(manifest) {
        let errors = errors.map(|error| error.to_string()).collect::<Vec<_>>();
        panic!(
            "{} should validate against schema:\n{}",
            manifest_name,
            errors.join("\n")
        );
    };
}

#[test]
fn qbit_fixture_manifest_example_keeps_contract_shape() {
    let schema: Value =
        serde_json::from_str(include_str!("fixtures/qbit/manifest.schema.json")).unwrap();
    let example: Value =
        serde_json::from_str(include_str!("fixtures/qbit/manifest.example.json")).unwrap();

    let config = Config::from_schema(&schema, Some(schemas::Draft::Draft7))
        .expect("manifest schema should compile");
    if let Err(errors) = config.validate_schema() {
        let errors = errors.map(|error| error.to_string()).collect::<Vec<_>>();
        panic!("manifest schema should be valid:\n{}", errors.join("\n"));
    }
    assert_manifest_valid(&schema, &example, "manifest example");

    assert_eq!(schema["title"], "qbit Fixture Manifest");
    assert_eq!(example["contract"]["document"], "doc/qbit-contract.md");
    assert_eq!(example["contract"]["qbit_commit"], QBIT_COMMIT);
    assert_eq!(example["fixtures"][0]["source"]["qbit_commit"], QBIT_COMMIT);

    let required = schema["definitions"]["fixture"]["required"]
        .as_array()
        .expect("fixture schema required fields");
    for field in [
        "id",
        "network",
        "fixture_type",
        "path",
        "source",
        "generation_command",
        "expected_parser_facts",
        "refresh_policy",
    ] {
        assert!(
            required.iter().any(|value| value.as_str() == Some(field)),
            "schema should require {}",
            field
        );
        assert!(
            example["fixtures"][0].get(field).is_some(),
            "example fixture should include {}",
            field
        );
    }

    let required_issues = schema["properties"]["contract"]["properties"]["issues"]["allOf"]
        .as_array()
        .expect("contract issues should require specific issue anchors");
    for issue in [12, 14, 22] {
        assert!(
            required_issues
                .iter()
                .any(|rule| rule["contains"]["const"].as_i64() == Some(issue)),
            "schema should require issue {}",
            issue
        );
    }

    let contract_doc = include_str!("../doc/qbit-contract.md");
    assert!(contract_doc.contains(QBIT_COMMIT));
    assert!(contract_doc.contains("testnet4` and `regtest`"));
    assert!(contract_doc.contains("Codec And Dependency Strategy"));
    assert!(contract_doc.contains("src/qbit_codec.rs"));
    assert!(contract_doc.contains("rust-bitcoin-qbit"));
    assert!(contract_doc.contains("WITNESS_SCALE_FACTOR"));
    assert!(contract_doc.contains("OP_CHECKSIGPQC"));
    assert!(contract_doc.contains("AuxPoW"));
}

#[test]
fn qbit_fixture_manifest_keeps_committed_fixtures_valid() {
    let schema: Value =
        serde_json::from_str(include_str!("fixtures/qbit/manifest.schema.json")).unwrap();
    let manifest: Value =
        serde_json::from_str(include_str!("fixtures/qbit/manifest.json")).unwrap();

    assert_manifest_valid(&schema, &manifest, "manifest");
    assert_eq!(manifest["contract"]["document"], "doc/qbit-contract.md");
    assert_eq!(manifest["contract"]["qbit_commit"], QBIT_COMMIT);
    assert_compatible_fixture_commits_are_explicit(&manifest);

    let fixtures = manifest["fixtures"]
        .as_array()
        .expect("manifest fixtures should be an array");
    assert!(fixtures.iter().any(|fixture| {
        fixture["fixture_type"] == "transaction"
            && fixture["expected_parser_facts"]["witness_scale_factor"] == 1
            && fixture["expected_parser_facts"]["qbit_weight"]
                == fixture["expected_parser_facts"]["serialized_size"]
    }));
    assert!(fixtures.iter().any(|fixture| {
        fixture["fixture_type"] == "block"
            && fixture["expected_parser_facts"]["auxpow"] == false
            && fixture["expected_parser_facts"]["pure_header_bytes"] == 80
    }));
    assert!(fixtures.iter().any(|fixture| {
        fixture["fixture_type"] == "block"
            && fixture["expected_parser_facts"]["auxpow"] == true
            && fixture["source"]["kind"] == "qbitd-generated"
            && fixture["expected_parser_facts"]["auxpow_extended_header_bytes"]
                .as_u64()
                .is_some()
    }));
    for id in [
        "regtest-qbitd-auxpow-block",
        "regtest-qbitd-auxpow-pure-header",
        "regtest-qbitd-auxpow-extended-header",
        "regtest-qbitd-auxpow-payload",
        "regtest-qbitd-getblock-auxpow",
        "regtest-qbitd-getblockheader-auxpow",
        "regtest-qbitd-createauxblock-template",
    ] {
        assert!(
            fixtures.iter().any(|fixture| fixture["id"] == id),
            "manifest should include promoted qbitd AuxPoW fixture {}",
            id
        );
    }
    for id in [
        "regtest-qbitd-p2mr-stack-16k-spend-tx",
        "regtest-qbitd-p2mr-stack-128k-spend-tx",
        "regtest-qbitd-p2mr-stack-item-oversize-reject-tx",
        "regtest-qbitd-p2mr-stack-total-oversize-reject-tx",
        "regtest-qbitd-p2mr-boundary-spends-block",
        "regtest-qbitd-testmempoolaccept-p2mr-stack-16k",
        "regtest-qbitd-testmempoolaccept-p2mr-stack-128k",
        "regtest-qbitd-testmempoolaccept-p2mr-stack-item-oversize",
        "regtest-qbitd-testmempoolaccept-p2mr-stack-total-oversize",
        "regtest-qbitd-getmempoolentry-p2mr-stack-16k",
        "regtest-qbitd-getmempoolentry-p2mr-stack-128k",
        "regtest-qbitd-getrawmempool-with-p2mr-boundary-spends",
        "regtest-qbitd-getblock-p2mr-boundary-spends",
        "regtest-qbitd-getblockheader-p2mr-boundary-spends",
        "regtest-qbitd-p2wsh-sigop-adjusted-vsize-spend-tx",
        "regtest-qbitd-testmempoolaccept-p2wsh-sigop-adjusted-vsize",
        "regtest-qbitd-getmempoolentry-p2wsh-sigop-adjusted-vsize",
        "regtest-qbitd-getrawtransaction-p2wsh-sigop-adjusted-vsize",
        "regtest-qbitd-getrawmempool-with-sigop-adjusted-vsize",
    ] {
        assert!(
            fixtures.iter().any(|fixture| fixture["id"] == id),
            "manifest should include promoted qbitd fixture {}",
            id
        );
    }
    assert!(fixtures.iter().any(|fixture| {
        fixture["id"] == "regtest-qbitd-p2mr-stack-128k-spend-tx"
            && fixture["expected_parser_facts"]["initial_stack_total_bytes"] == 128 * 1024
            && fixture["expected_parser_facts"]["qbit_weight"]
                == fixture["expected_parser_facts"]["serialized_size"]
    }));
    assert!(fixtures.iter().any(|fixture| {
        fixture["id"] == "regtest-qbitd-p2wsh-sigop-adjusted-vsize-spend-tx"
            && fixture["expected_parser_facts"]["serialized_size"] == 115
            && fixture["expected_parser_facts"]["qbit_tx_vsize"] == 115
            && fixture["expected_parser_facts"]["mempool_vsize"] == 4440
            && fixture["expected_parser_facts"]["mempool_vsize_exceeds_serialized_size"] == true
            && fixture["expected_parser_facts"]["p2mr_witness"] == false
    }));
    assert!(fixtures.iter().any(|fixture| {
        fixture["fixture_type"] == "block"
            && fixture["expected_parser_facts"]["auxpow"] == true
            && fixture["source"]["kind"] == "synthetic"
    }));
    let address_fixtures = fixtures
        .iter()
        .filter(|fixture| fixture["fixture_type"] == "address")
        .collect::<Vec<_>>();
    assert_eq!(address_fixtures.len(), 3);
    for (network, hrp, address) in [
        (
            "mainnet",
            "qb",
            "qb1zqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqm5lumh",
        ),
        (
            "testnet4",
            "tq",
            "tq1zqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqdagu6k",
        ),
        (
            "regtest",
            "qbrt",
            "qbrt1zqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqwztzna",
        ),
    ] {
        let fixture = address_fixtures
            .iter()
            .find(|fixture| fixture["network"] == network)
            .unwrap_or_else(|| panic!("missing {} P2MR address fixture", network));
        assert_eq!(fixture["address"], address);
        assert_eq!(
            fixture["script"],
            "52200000000000000000000000000000000000000000000000000000000000000000"
        );
        assert_eq!(fixture["expected_parser_facts"]["network_hrp"], hrp);
        assert_eq!(fixture["expected_parser_facts"]["witness_version"], 2);
        assert_eq!(
            fixture["expected_parser_facts"]["witness_program_bytes"],
            32
        );

        let fixture_file: Value = serde_json::from_str(
            &std::fs::read_to_string(fixture["path"].as_str().unwrap()).unwrap(),
        )
        .unwrap();
        assert_eq!(fixture_file["p2mr"]["address"], address);
        assert_eq!(fixture_file["p2mr"]["hrp"], hrp);
        assert_eq!(fixture_file["p2mr"]["bech32_encoding"], "bech32m");
        assert_eq!(fixture_file["p2mr"]["script_pubkey_hex"], fixture["script"]);
        assert_eq!(
            fixture_file["wrong_network_rejection_vectors"]
                .as_array()
                .unwrap()
                .len(),
            2
        );
    }
    assert!(fixtures.iter().any(|fixture| {
        fixture["fixture_type"] == "db-row"
            && fixture["expected_parser_facts"]["row_type"] == "TxHistoryRow"
            && fixture["expected_parser_facts"]["script_hash"]
                == "379bfa9706fc3e3b0dbe80ff6615a46c781ca7124daf93e513c0f77f5bb12257"
    }));

    for fixture in fixtures {
        let path = fixture["path"].as_str().expect("fixture path");
        assert!(
            std::path::Path::new(path).exists(),
            "fixture path should exist: {}",
            path
        );
    }
}

fn assert_compatible_fixture_commits_are_explicit(manifest: &Value) {
    let contract_commit = manifest["contract"]["qbit_commit"]
        .as_str()
        .expect("contract qbit commit");
    let compatible_commits = manifest["contract"]["compatible_fixture_commits"]
        .as_array()
        .expect("compatible fixture commits should be explicit");
    assert_eq!(contract_commit, QBIT_COMMIT);
    assert!(compatible_commits.iter().any(|entry| {
        entry["qbit_commit"] == LEGACY_P2MR_QBIT_COMMIT
            && entry["scope"]
                .as_str()
                .map(|scope| scope.contains("P2MR"))
                .unwrap_or(false)
    }));

    let fixtures = manifest["fixtures"]
        .as_array()
        .expect("manifest fixtures should be an array");
    for fixture in fixtures {
        let id = fixture["id"].as_str().expect("fixture id");
        let source_commit = fixture["source"]["qbit_commit"]
            .as_str()
            .expect("fixture source qbit commit");
        if source_commit == contract_commit {
            continue;
        }

        let compatible_entry = compatible_commits
            .iter()
            .find(|entry| {
                entry["qbit_commit"] == source_commit
                    && entry["fixture_ids"]
                        .as_array()
                        .map(|ids| ids.iter().any(|value| value.as_str() == Some(id)))
                        .unwrap_or(false)
            })
            .unwrap_or_else(|| {
                panic!(
                    "fixture {} uses non-contract qbit commit {} without compatible_fixture_commits entry",
                    id, source_commit
                )
            });
        assert_eq!(source_commit, LEGACY_P2MR_QBIT_COMMIT);
        assert_eq!(fixture["source"]["kind"], "qbitd-generated");
        assert!(
            id.contains("p2mr"),
            "legacy qbit commit should only be used for P2MR qbitd fixtures: {}",
            id
        );
        assert!(
            compatible_entry["reason"]
                .as_str()
                .map(|reason| reason.contains("unchanged"))
                .unwrap_or(false),
            "compatible fixture commit should explain unchanged semantics"
        );
    }
}

#[test]
fn qbitd_export_manifest_example_documents_real_fixture_bundle() {
    let schema: Value =
        serde_json::from_str(include_str!("fixtures/qbit/manifest.schema.json")).unwrap();
    let export_example: Value = serde_json::from_str(include_str!(
        "fixtures/qbit/qbitd-export-manifest.example.json"
    ))
    .unwrap();

    assert_manifest_valid(&schema, &export_example, "qbitd export manifest example");
    assert_eq!(
        export_example["contract"]["document"],
        "doc/qbit-contract.md"
    );
    assert_eq!(export_example["contract"]["qbit_commit"], QBIT_COMMIT);

    let fixtures = export_example["fixtures"]
        .as_array()
        .expect("qbitd export fixtures should be an array");
    assert!(fixtures.iter().all(|fixture| {
        fixture["source"]["kind"] == "qbitd-generated"
            && fixture["source"]["qbit_commit"] == QBIT_COMMIT
            && fixture["generation_command"]
                .as_str()
                .map(|command| command.contains("--export-qbit-fixtures"))
                .unwrap_or(false)
    }));
    assert!(fixtures.iter().any(|fixture| {
        fixture["fixture_type"] == "transaction"
            && fixture["expected_parser_facts"]["p2mr_witness"] == true
            && fixture["expected_parser_facts"]["witness_scale_factor"] == 1
            && fixture["expected_parser_facts"]["qbit_weight"]
                == fixture["expected_parser_facts"]["serialized_size"]
    }));
    assert!(fixtures.iter().any(|fixture| {
        fixture["fixture_type"] == "block"
            && fixture["expected_parser_facts"]["pure_header_bytes"] == 80
            && fixture["expected_parser_facts"]["contains_txid"]
                == "1111111111111111111111111111111111111111111111111111111111111111"
    }));
    assert!(fixtures.iter().any(|fixture| {
        fixture["fixture_type"] == "rpc"
            && fixture["expected_parser_facts"]["method"] == "getmempoolentry"
            && fixture["expected_parser_facts"]["vsize_policy"]
                == "qbit WSF=1 witness-inclusive serialized size"
    }));
}

#[test]
fn qbit_fixture_generator_reproduces_committed_files() {
    let status = Command::new("python3")
        .args([
            "tests/fixtures/qbit/scripts/generate_qbit_fixtures.py",
            "--check",
        ])
        .status()
        .expect("fixture generator should run");

    assert!(status.success(), "fixture generator --check should pass");
}
